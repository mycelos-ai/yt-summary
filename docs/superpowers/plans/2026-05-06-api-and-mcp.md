# API + MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a REST API at `/api/v1/*` and an MCP server at `/mcp/sse`, both authenticated by a single API key tied to the new `users` table, sharing one service-layer over the existing repos.

**Architecture:** A new `services/api.py` aggregates existing repos+services into user-scoped operations. Two thin route modules (`routes/api.py` for REST/JSON, `routes/mcp.py` for MCP) translate between protocol and the shared layer. API-key auth is a FastAPI dependency that falls back to user 1 when no key is configured.

**Tech Stack:** FastAPI (already there), `mcp` Python SDK (new dependency, official Anthropic package), aiosqlite, existing repos.

**Reference:** [docs/superpowers/specs/2026-05-06-api-and-mcp-design.md](../specs/2026-05-06-api-and-mcp-design.md)

---

## File Structure

```
app/
  db.py                  # MODIFY: users table in SCHEMA + migration
  models.py              # MODIFY: User dataclass
  repos/
    users.py             # NEW: get_default_user, set/clear/find by api_key_hash
  services/
    auth.py              # NEW: generate_api_key, authenticate dependency
    api.py               # NEW: shared facade — submit_video, get_video_resource,
                         #      list_videos, search_videos, chat_about_video,
                         #      list_playlists, create_playlist, refresh_playlist,
                         #      load_older, remove_playlist, list_tags, list_recent
  routes/
    api.py               # NEW: /api/v1/* JSON endpoints (FastAPI sub-router)
    mcp.py               # NEW: /mcp/sse — MCP server + tool registration
    settings.py          # MODIFY: API key generate/regenerate/revoke
  main.py                # MODIFY: include api_router + mcp_router; boot warning
                         #         when no API key set
  templates/
    settings.html        # MODIFY: API access section
pyproject.toml           # MODIFY: add 'mcp' dependency
README.md                # MODIFY: link to /api/v1/docs and MCP setup blurb

tests/
  test_repos_users.py            # NEW
  test_services_auth.py          # NEW
  test_services_api.py           # NEW (smoke-tests for the facade)
  test_routes_api_videos.py      # NEW
  test_routes_api_search.py      # NEW
  test_routes_api_playlists.py   # NEW
  test_routes_api_tags.py        # NEW
  test_routes_api_health.py      # NEW
  test_routes_mcp.py             # NEW (in-process MCP tool dispatch)
  test_routes_settings.py        # MODIFY: API key UI
  test_db.py                     # MODIFY: users table + default-user seeding
```

**Responsibility split:**
- `repos/users.py` does only DB access. No hashing, no key generation.
- `services/auth.py` owns the cryptography (key gen, hashing, header parsing) and the FastAPI dependency function.
- `services/api.py` is the protocol-agnostic business layer. It knows about videos and chats and playlists, but nothing about HTTP or MCP.
- `routes/api.py` is the HTTP/JSON adapter. Each handler is small: parse → call services/api → serialize.
- `routes/mcp.py` is the MCP adapter. Each tool is small: validate args → call services/api → return Python value (the MCP SDK does JSON serialization).

---

## Phase 1: Schema + Users Repo

### Task 1.1: users table + migration

**Files:**
- Modify: `app/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Add the table to SCHEMA**

In `app/db.py`, add this block to `SCHEMA` (after the `tags`/`video_tags`/`video_embeddings` blocks, before `videos_fts`):

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT 'admin',
    api_key_hash TEXT,
    api_key_prefix TEXT,
    api_key_created_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- [ ] **Step 2: Seed the default user in `_run_migrations`**

After the migrations and before the `await conn.commit()`, append:

```python
    # Seed the single default user (id=1) if the table is empty. Every
    # existing user_id=1 reference now points at a real row.
    if await _table_exists(conn, "users"):
        cursor = await conn.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        if row is not None and row[0] == 0:
            await conn.execute(
                "INSERT INTO users (id, name) VALUES (1, 'admin')"
            )
```

- [ ] **Step 3: Write failing tests**

Append to `tests/test_db.py`:

```python
async def test_init_schema_creates_users_table(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    )
    assert await cursor.fetchone() is not None


async def test_init_schema_seeds_default_user(db: aiosqlite.Connection):
    cursor = await db.execute("SELECT id, name FROM users WHERE id = 1")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == "admin"


async def test_init_schema_default_user_has_no_key(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT api_key_hash, api_key_prefix FROM users WHERE id = 1"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
```

- [ ] **Step 4: Run, verify pass**

```bash
.venv/bin/pytest tests/test_db.py -v
.venv/bin/pytest -q
```

Expected: 3 new tests pass; full suite still 218 + 3 = 221.

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check app tests
git add app/db.py tests/test_db.py
git commit -m "feat(db): users table + default-user seeding"
```

### Task 1.2: User dataclass

**Files:**
- Modify: `app/models.py`
- Modify: `tests/test_models.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_models.py`:

```python
def test_user_dataclass():
    from app.models import User
    u = User(
        id=1,
        name="admin",
        api_key_hash=None,
        api_key_prefix=None,
        api_key_created_at=None,
        created_at=datetime(2026, 5, 6),
    )
    assert u.id == 1
    assert u.name == "admin"
    assert u.api_key_hash is None
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/test_models.py::test_user_dataclass -v
```

Expected: FAIL (ImportError).

- [ ] **Step 3: Add the dataclass**

Append to `app/models.py`:

```python
@dataclass
class User:
    id: int
    name: str
    api_key_hash: str | None
    api_key_prefix: str | None
    api_key_created_at: datetime | None
    created_at: datetime
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_models.py -v
.venv/bin/ruff check app tests
git add app/models.py tests/test_models.py
git commit -m "feat(models): User dataclass"
```

### Task 1.3: users repo

**Files:**
- Create: `app/repos/users.py`
- Create: `tests/test_repos_users.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_repos_users.py`:

```python
import aiosqlite

from app.repos import users as users_repo


async def test_get_default_user_returns_seeded_user(db: aiosqlite.Connection):
    user = await users_repo.get_default_user(db)
    assert user is not None
    assert user.id == 1
    assert user.name == "admin"
    assert user.api_key_hash is None


async def test_set_api_key_persists_hash_and_prefix(db: aiosqlite.Connection):
    await users_repo.set_api_key(
        db, user_id=1, key_hash="sha256-of-key", key_prefix="yts_xQ4f"
    )
    user = await users_repo.get_default_user(db)
    assert user is not None
    assert user.api_key_hash == "sha256-of-key"
    assert user.api_key_prefix == "yts_xQ4f"
    assert user.api_key_created_at is not None


async def test_clear_api_key_resets_fields(db: aiosqlite.Connection):
    await users_repo.set_api_key(
        db, user_id=1, key_hash="h", key_prefix="p"
    )
    await users_repo.clear_api_key(db, user_id=1)
    user = await users_repo.get_default_user(db)
    assert user is not None
    assert user.api_key_hash is None
    assert user.api_key_prefix is None
    assert user.api_key_created_at is None


async def test_find_by_api_key_hash_returns_user(db: aiosqlite.Connection):
    await users_repo.set_api_key(
        db, user_id=1, key_hash="hash-aaa", key_prefix="yts_aaaa"
    )
    found = await users_repo.find_by_api_key_hash(db, "hash-aaa")
    assert found is not None
    assert found.id == 1


async def test_find_by_api_key_hash_returns_none_for_unknown(db: aiosqlite.Connection):
    found = await users_repo.find_by_api_key_hash(db, "no-such-hash")
    assert found is None
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/test_repos_users.py -v
```

Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement `app/repos/users.py`**

```python
from datetime import datetime

import aiosqlite

from app.models import User


def _row_to_user(row: aiosqlite.Row) -> User:
    created_at = row["api_key_created_at"]
    return User(
        id=row["id"],
        name=row["name"],
        api_key_hash=row["api_key_hash"],
        api_key_prefix=row["api_key_prefix"],
        api_key_created_at=datetime.fromisoformat(created_at) if created_at else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def get_default_user(db: aiosqlite.Connection) -> User | None:
    cursor = await db.execute("SELECT * FROM users WHERE id = 1")
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None


async def set_api_key(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    key_hash: str,
    key_prefix: str,
) -> None:
    await db.execute(
        """
        UPDATE users SET
            api_key_hash = ?,
            api_key_prefix = ?,
            api_key_created_at = datetime('now')
        WHERE id = ?
        """,
        (key_hash, key_prefix, user_id),
    )
    await db.commit()


async def clear_api_key(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        """
        UPDATE users SET
            api_key_hash = NULL,
            api_key_prefix = NULL,
            api_key_created_at = NULL
        WHERE id = ?
        """,
        (user_id,),
    )
    await db.commit()


async def find_by_api_key_hash(
    db: aiosqlite.Connection, key_hash: str
) -> User | None:
    cursor = await db.execute(
        "SELECT * FROM users WHERE api_key_hash = ?", (key_hash,)
    )
    row = await cursor.fetchone()
    return _row_to_user(row) if row else None
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_repos_users.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/repos/users.py tests/test_repos_users.py
git commit -m "feat(repos): users repo (api key set/clear/find)"
```

---

## Phase 2: Auth service

### Task 2.1: API key generation + hashing

**Files:**
- Create: `app/services/auth.py`
- Create: `tests/test_services_auth.py`

- [ ] **Step 1: Failing tests**

Create `tests/test_services_auth.py`:

```python
import pytest


def test_generate_api_key_returns_three_pieces():
    from app.services.auth import generate_api_key
    plaintext, key_hash, prefix = generate_api_key()
    assert isinstance(plaintext, str)
    assert plaintext.startswith("yts_")
    assert len(plaintext) > 30
    assert isinstance(key_hash, str)
    assert len(key_hash) == 64  # sha256 hex
    assert prefix == plaintext[:8]


def test_generate_api_key_is_random():
    from app.services.auth import generate_api_key
    a, _, _ = generate_api_key()
    b, _, _ = generate_api_key()
    assert a != b


def test_hash_api_key_is_deterministic():
    from app.services.auth import hash_api_key
    h1 = hash_api_key("yts_abc123")
    h2 = hash_api_key("yts_abc123")
    assert h1 == h2
    assert len(h1) == 64
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/test_services_auth.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement key generation + hashing**

Create `app/services/auth.py`:

```python
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
from fastapi import Header, HTTPException, Request

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
```

- [ ] **Step 4: Run, verify pass + commit**

```bash
.venv/bin/pytest tests/test_services_auth.py -v
git add app/services/auth.py tests/test_services_auth.py
git commit -m "feat(auth): generate_api_key + hash_api_key helpers"
```

### Task 2.2: `authenticate` FastAPI dependency

**Files:**
- Modify: `app/services/auth.py`
- Modify: `tests/test_services_auth.py`

- [ ] **Step 1: Failing tests**

Append to `tests/test_services_auth.py`:

```python
from unittest.mock import AsyncMock, MagicMock


async def _fake_request(headers: dict[str, str]) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    return req


async def test_authenticate_returns_default_user_when_no_key_configured(
    db,
):
    from app.services.auth import authenticate
    req = await _fake_request({})
    user_id = await authenticate(db, req)
    assert user_id == 1


async def test_authenticate_with_valid_bearer(db):
    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    plaintext = "yts_test123abc"
    await users_repo.set_api_key(
        db, user_id=1, key_hash=hash_api_key(plaintext), key_prefix=plaintext[:8]
    )
    req = await _fake_request({"authorization": f"Bearer {plaintext}"})
    user_id = await authenticate(db, req)
    assert user_id == 1


async def test_authenticate_with_valid_x_api_key(db):
    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    plaintext = "yts_xyzqwerty"
    await users_repo.set_api_key(
        db, user_id=1, key_hash=hash_api_key(plaintext), key_prefix=plaintext[:8]
    )
    req = await _fake_request({"x-api-key": plaintext})
    user_id = await authenticate(db, req)
    assert user_id == 1


async def test_authenticate_rejects_wrong_key(db):
    import pytest
    from fastapi import HTTPException
    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    await users_repo.set_api_key(
        db,
        user_id=1,
        key_hash=hash_api_key("yts_correct"),
        key_prefix="yts_corr",
    )
    req = await _fake_request({"authorization": "Bearer yts_wrong"})
    with pytest.raises(HTTPException) as exc:
        await authenticate(db, req)
    assert exc.value.status_code == 401


async def test_authenticate_rejects_missing_when_key_required(db):
    import pytest
    from fastapi import HTTPException
    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    await users_repo.set_api_key(
        db,
        user_id=1,
        key_hash=hash_api_key("yts_yes"),
        key_prefix="yts_yes_",
    )
    req = await _fake_request({})
    with pytest.raises(HTTPException) as exc:
        await authenticate(db, req)
    assert exc.value.status_code == 401
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/test_services_auth.py -v
```

Expected: 5 new tests fail (NameError on authenticate).

- [ ] **Step 3: Add `authenticate` to `app/services/auth.py`**

Append to `app/services/auth.py`:

```python
def _extract_token(headers: dict | "Headers") -> str | None:
    """Pull the API key out of the request headers.

    Accepts both `Authorization: Bearer <token>` and `X-API-Key: <token>`.
    Headers are case-insensitive.
    """
    auth = None
    # Both real Headers (case-insensitive) and our test dicts work via .get
    if hasattr(headers, "get"):
        auth = headers.get("authorization") or headers.get("Authorization")
        if not auth:
            return headers.get("x-api-key") or headers.get("X-API-Key")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def authenticate(
    db: aiosqlite.Connection, request: Request
) -> int:
    """FastAPI dependency: validate API key, return user_id.

    If no api_key is configured for any user, returns user_id=1 (the
    default user). Otherwise, requires a matching Bearer / X-API-Key
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
```

- [ ] **Step 4: Run, verify pass + lint**

```bash
.venv/bin/pytest tests/test_services_auth.py -v
.venv/bin/ruff check app tests
```

- [ ] **Step 5: Commit**

```bash
git add app/services/auth.py tests/test_services_auth.py
git commit -m "feat(auth): authenticate dependency with bearer + X-API-Key"
```

---

## Phase 3: Settings UI for API key

### Task 3.1: Generate, regenerate, revoke endpoints

**Files:**
- Modify: `app/routes/settings.py`
- Modify: `app/templates/settings.html`
- Modify: `tests/test_routes_settings.py`

- [ ] **Step 1: Failing tests**

Append to `tests/test_routes_settings.py`:

```python
def test_generate_api_key_creates_key_and_shows_once(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/api-key/generate", follow_redirects=False
        )
    assert resp.status_code == 200
    assert "yts_" in resp.text
    # Plaintext shown once
    assert "shown only once" in resp.text.lower() or "show only once" in resp.text.lower()


def test_settings_page_shows_api_key_prefix_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post("/settings/api-key/generate", follow_redirects=False)
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "yts_" in resp.text
    assert "..." in resp.text
    # Plaintext is NOT shown after first reveal
    # (we can't assert this strictly without the plaintext, but the
    # prefix-only display means the rest is masked)


def test_revoke_api_key_clears_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        client.post("/settings/api-key/generate", follow_redirects=False)
        resp = client.post(
            "/settings/api-key/revoke", follow_redirects=False
        )
        assert resp.status_code in (200, 303)

        import asyncio
        async def check():
            from app.repos import users as users_repo
            user = await users_repo.get_default_user(app.state.db)
            assert user is not None
            assert user.api_key_hash is None

        asyncio.get_event_loop().run_until_complete(check())
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Add three routes**

Append to `app/routes/settings.py`:

```python
from app.repos import users as users_repo
from app.services.auth import generate_api_key as _gen_key


@router.post("/settings/api-key/generate", response_class=HTMLResponse)
async def generate_api_key_route(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    plaintext, key_hash, prefix = _gen_key()
    await users_repo.set_api_key(
        db, user_id=1, key_hash=key_hash, key_prefix=prefix
    )
    return templates.TemplateResponse(
        request,
        "api_key_reveal.html",
        {"plaintext": plaintext, "prefix": prefix},
    )


@router.post("/settings/api-key/revoke")
async def revoke_api_key_route(
    db: aiosqlite.Connection = Depends(get_db),
):
    await users_repo.clear_api_key(db, user_id=1)
    return RedirectResponse("/settings", status_code=303)
```

- [ ] **Step 4: Create `app/templates/api_key_reveal.html`**

```html
{% extends "base.html" %}
{% block title %}API key generated — yt-summary{% endblock %}
{% block content %}
<div class="settings-page">
  <h1>Your new API key</h1>
  <p style="color: var(--brand-error); font-weight: 500;">
    This key is shown only once. Copy it now — we only store its hash.
  </p>
  <pre style="background: var(--surface); padding: 16px; border-radius: var(--rounded-md); user-select: all;">{{ plaintext }}</pre>
  <p>
    <a href="/settings" class="btn btn-primary">I've copied it</a>
  </p>
</div>
{% endblock %}
```

- [ ] **Step 5: Modify `app/templates/settings.html` — add API access section**

Insert after the YouTube cookies form, before the closing `</div>`:

```html
<p class="section-title" style="margin-top: 48px;">API access</p>

<div class="api-access-block" style="background: var(--canvas); border: 1px solid var(--hairline); border-radius: var(--rounded-lg); padding: 24px;">
  {% if api_key_prefix %}
    <p>
      <strong>Status:</strong> ● Active &mdash;
      <code>{{ api_key_prefix }}...</code>
      {% if api_key_created_at %}
        <span style="color: var(--steel);">created {{ api_key_created_at.strftime('%Y-%m-%d') }}</span>
      {% endif %}
    </p>
    <form method="post" action="/settings/api-key/generate" style="display:inline">
      <button type="submit" class="btn btn-secondary">Regenerate</button>
    </form>
    <form method="post" action="/settings/api-key/revoke" style="display:inline"
          onsubmit="return confirm('Revoke the current API key? Any tools using it will stop working.')">
      <button type="submit" class="btn btn-secondary">Revoke</button>
    </form>
  {% else %}
    <p>
      <strong>Status:</strong> ⚠ No API key — anyone on the LAN can call the API.
    </p>
    <form method="post" action="/settings/api-key/generate" style="display:inline">
      <button type="submit" class="btn btn-primary">Generate API Key</button>
    </form>
  {% endif %}

  <ul style="margin-top: 24px; color: var(--steel); font-size: 13px; padding-left: 20px;">
    <li>REST API: <code>/api/v1/</code> &middot; <a href="/api/v1/docs" target="_blank">OpenAPI docs</a></li>
    <li>MCP server: <code>/mcp/sse</code></li>
  </ul>
</div>
```

- [ ] **Step 6: Update the GET /settings handler to fetch user info**

In `app/routes/settings.py`, modify `settings_page` to also pass `api_key_prefix` and `api_key_created_at`:

```python
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    settings = await settings_repo.get_all(db)
    has_cookies = await asyncio.to_thread(config.cookies_path.exists)
    has_api_key = bool(settings.get("llm_api_key"))
    safe_settings = {k: v for k, v in settings.items() if k != "llm_api_key"}
    user = await users_repo.get_default_user(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": safe_settings,
            "has_api_key": has_api_key,
            "has_cookies": has_cookies,
            "api_key_prefix": user.api_key_prefix if user else None,
            "api_key_created_at": user.api_key_created_at if user else None,
        },
    )
```

- [ ] **Step 7: Run tests + lint + commit**

```bash
.venv/bin/pytest tests/test_routes_settings.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/settings.py app/templates/settings.html app/templates/api_key_reveal.html tests/test_routes_settings.py
git commit -m "feat(settings): API key generate / revoke UI"
```

---

## Phase 4: Shared API service layer

### Task 4.1: services/api.py — submit + get + list

**Files:**
- Create: `app/services/api.py`
- Create: `tests/test_services_api.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_services_api.py`:

```python
from unittest.mock import AsyncMock, patch

from app.config import Config
from app.repos import videos as videos_repo


async def test_get_video_resource_returns_dict(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="vapi1", url="https://x", title="T",
        description="d", thumbnail_path=None, duration_seconds=300,
    )
    from app.services.api import get_video_resource
    resource = await get_video_resource(db, "vapi1")
    assert resource is not None
    assert resource["id"] == "vapi1"
    assert resource["title"] == "T"
    assert resource["summary_ready"] is False
    assert resource["kind"] == "youtube"
    assert resource["url"] == "https://x"
    assert resource["thumbnail_url"] is None  # no file on disk


async def test_get_video_resource_returns_none_for_unknown(db, tmp_path):
    from app.services.api import get_video_resource
    assert await get_video_resource(db, "nope") is None


async def test_list_videos_paginates(db, tmp_path):
    for i in range(5):
        await videos_repo.upsert_metadata(
            db, video_id=f"vapi{i}", url="u", title=f"V{i}",
            description="", thumbnail_path=None, duration_seconds=None,
        )
    from app.services.api import list_videos
    page1 = await list_videos(db, limit=2, offset=0)
    page2 = await list_videos(db, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {v["id"] for v in page1}.isdisjoint({v["id"] for v in page2})


async def test_submit_video_async_returns_pending(db, tmp_path):
    from app.services.api import submit_video
    from app.services.youtube import VideoMetadata

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    fake_meta = VideoMetadata(
        id="newvid12345",
        url="https://youtu.be/newvid12345",
        title="New",
        description="",
        duration_seconds=120,
        thumbnail_url=None,
    )

    with (
        patch("app.services.api.fetch_metadata", AsyncMock(return_value=fake_meta)),
        patch("app.services.api.download_thumbnail", AsyncMock(return_value=None)),
    ):
        result = await submit_video(
            db, config, url="https://youtu.be/newvid12345",
            user_id=1, wait=False, timeout=0,
        )
    assert result["video_id"] == "newvid12345"
    assert result["summary_ready"] is False
    assert result["kind"] == "youtube"
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/test_services_api.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement core of `app/services/api.py`**

Create `app/services/api.py`:

```python
"""Shared service layer for the REST API and MCP server.

Each function takes the db connection + parameters, returns a plain
Python value (dict / list / str). No HTTP, no MCP. Both surface
adapters serialize from these returns.
"""

import asyncio
from typing import Any, TypedDict

import aiosqlite

from app.config import Config
from app.models import VideoKind
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import tags as tags_repo
from app.repos import videos as videos_repo
from app.services.reader import fetch_article
from app.services.url_classify import classify_url, web_id_from_url
from app.services.youtube import download_thumbnail, fetch_metadata


class VideoResource(TypedDict, total=False):
    id: str
    kind: str
    url: str
    title: str
    description: str
    thumbnail_url: str | None
    duration_seconds: int | None
    transcript_source: str | None
    summary_model: str | None
    summary_ready: bool
    tags: list[str]
    playlists: list[dict]
    job: dict | None
    created_at: str
    updated_at: str


def _video_to_resource(
    video, *, tag_names: list[str] | None = None,
    playlist_links: list[tuple[str, str]] | None = None,
    job=None, elapsed_s: int | None = None,
) -> VideoResource:
    return {
        "id": video.id,
        "kind": video.kind.value,
        "url": video.url,
        "title": video.title,
        "description": video.description,
        "thumbnail_url": (
            f"/thumbnails/{video.id}.jpg" if video.thumbnail_path else None
        ),
        "duration_seconds": video.duration_seconds,
        "transcript_source": (
            video.transcript_source.value if video.transcript_source else None
        ),
        "summary_model": video.summary_model,
        "summary_ready": bool(video.summary),
        "tags": tag_names or [],
        "playlists": [
            {"id": pid, "title": ptitle}
            for pid, ptitle in (playlist_links or [])
        ],
        "job": (
            {
                "state": job.state.value,
                "step": job.step,
                "error_message": job.error_message,
                "elapsed_seconds": elapsed_s,
            }
            if job else None
        ),
        "created_at": video.created_at.isoformat(),
        "updated_at": video.updated_at.isoformat(),
    }


async def get_video_resource(
    db: aiosqlite.Connection, video_id: str
) -> VideoResource | None:
    video = await videos_repo.get(db, video_id)
    if video is None:
        return None
    tags = await tags_repo.tags_for_video(db, video_id)
    plinks_map = await playlists_repo.playlists_for_videos(db, [video_id])
    plinks = plinks_map.get(video_id, [])
    job = await jobs_repo.latest_for_video(db, video_id)
    return _video_to_resource(
        video, tag_names=tags, playlist_links=plinks, job=job
    )


async def list_videos(
    db: aiosqlite.Connection,
    limit: int = 50,
    offset: int = 0,
    *,
    tag: str | None = None,
    playlist_id: str | None = None,
) -> list[VideoResource]:
    if playlist_id:
        videos = await playlists_repo.videos_for_playlist(db, playlist_id)
        videos = videos[offset : offset + limit]
    else:
        videos = await videos_repo.list_recent(db, limit=limit + offset, tag=tag)
        videos = videos[offset : offset + limit]
    if not videos:
        return []
    ids = [v.id for v in videos]
    tags_map = await tags_repo.tags_for_videos(db, ids)
    plinks_map = await playlists_repo.playlists_for_videos(db, ids)
    return [
        _video_to_resource(
            v,
            tag_names=tags_map.get(v.id, []),
            playlist_links=plinks_map.get(v.id, []),
        )
        for v in videos
    ]


async def submit_video(
    db: aiosqlite.Connection,
    config: Config,
    *,
    url: str,
    user_id: int,
    wait: bool = False,
    timeout: int = 60,
) -> VideoResource:
    """Submit a URL. Async by default; sync waits up to `timeout` seconds
    for the summary to finish."""
    from app.repos import tags as _tags_repo  # local: avoid cycle import noise
    from app.models import TranscriptSource as _TS

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Not an http(s) URL: {url!r}")

    cookies = config.cookies_path if config.cookies_path.exists() else None

    if classify_url(url) == "youtube":
        meta = await fetch_metadata(url, cookies_path=cookies)
        thumb_target = config.thumbnails_dir / f"{meta.id}.jpg"
        await download_thumbnail(meta.thumbnail_url, thumb_target)
        thumb_db_path = str(thumb_target) if thumb_target.exists() else None
        await videos_repo.upsert_metadata(
            db,
            video_id=meta.id,
            url=meta.url,
            title=meta.title,
            description=meta.description,
            thumbnail_path=thumb_db_path,
            duration_seconds=meta.duration_seconds,
            user_id=user_id,
            kind=VideoKind.YOUTUBE,
        )
        if meta.tags:
            await _tags_repo.set_tags_for_video(db, meta.id, list(meta.tags))
        await jobs_repo.enqueue(db, meta.id)
        item_id = meta.id
    else:
        article = await fetch_article(url)
        item_id = web_id_from_url(article.url)
        thumb_target = config.thumbnails_dir / f"{item_id}.jpg"
        thumb_db_path: str | None = None
        if article.thumbnail_url:
            try:
                await download_thumbnail(article.thumbnail_url, thumb_target)
                if thumb_target.exists():
                    thumb_db_path = str(thumb_target)
            except Exception:
                pass
        await videos_repo.upsert_metadata(
            db,
            video_id=item_id,
            url=article.url,
            title=article.title,
            description=article.description,
            thumbnail_path=thumb_db_path,
            duration_seconds=None,
            user_id=user_id,
            kind=VideoKind.WEB,
        )
        await videos_repo.set_transcript(db, item_id, article.body, _TS.WEB)
        await jobs_repo.enqueue(db, item_id)

    if wait and timeout > 0:
        await _wait_for_summary(db, item_id, timeout)

    resource = await get_video_resource(db, item_id)
    assert resource is not None
    return resource


async def _wait_for_summary(
    db: aiosqlite.Connection, video_id: str, timeout: int
) -> None:
    """Poll videos.summary every second up to `timeout` seconds."""
    deadline_iters = max(1, min(timeout, 300))
    for _ in range(deadline_iters):
        video = await videos_repo.get(db, video_id)
        if video and video.summary:
            return
        # also stop early if the latest job failed
        job = await jobs_repo.latest_for_video(db, video_id)
        if job and job.state.value == "failed":
            return
        await asyncio.sleep(1.0)
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_services_api.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/services/api.py tests/test_services_api.py
git commit -m "feat(api): shared service layer (submit, get, list)"
```

### Task 4.2: search, chat, playlists, tags helpers in services/api.py

**Files:**
- Modify: `app/services/api.py`
- Modify: `tests/test_services_api.py`

- [ ] **Step 1: Failing tests**

Append to `tests/test_services_api.py`:

```python
async def test_list_tags_returns_counts(db, tmp_path):
    from app.repos import tags as tags_repo
    await videos_repo.upsert_metadata(
        db, video_id="t1", url="u", title="t1",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="t2", url="u", title="t2",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await tags_repo.set_tags_for_video(db, "t1", ["python"])
    await tags_repo.set_tags_for_video(db, "t2", ["python", "fastapi"])

    from app.services.api import list_tags
    result = await list_tags(db)
    by_name = {t["name"]: t["count"] for t in result}
    assert by_name["python"] == 2
    assert by_name["fastapi"] == 1


async def test_list_playlists_resource_shape(db, tmp_path):
    from app.repos import playlists as playlists_repo
    await playlists_repo.create(
        db, playlist_id="PLapi1", user_id=1, url="u",
        title="Show", description="", thumbnail_path=None,
    )
    from app.services.api import list_playlists
    rows = await list_playlists(db, user_id=1)
    assert len(rows) == 1
    assert rows[0]["id"] == "PLapi1"
    assert rows[0]["title"] == "Show"
    assert rows[0]["video_count"] == 0
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Append to `app/services/api.py`**

```python
async def search_videos(
    db: aiosqlite.Connection,
    query: str,
    limit: int = 20,
    *,
    tag: str | None = None,
) -> list[VideoResource]:
    """Hybrid search through the videos repo, with optional tag filter.

    Uses the existing search() which does FTS5 + (when callable provides
    them) embedding fusion. The route-layer is responsible for embedding
    the query — here we just call the FTS path. Callers wanting the
    embedding component should follow the home.py pattern.
    """
    videos = await videos_repo.search(db, query, limit=limit, tag=tag)
    if not videos:
        return []
    ids = [v.id for v in videos]
    tags_map = await tags_repo.tags_for_videos(db, ids)
    plinks_map = await playlists_repo.playlists_for_videos(db, ids)
    return [
        _video_to_resource(
            v,
            tag_names=tags_map.get(v.id, []),
            playlist_links=plinks_map.get(v.id, []),
        )
        for v in videos
    ]


async def chat_about_video(
    db: aiosqlite.Connection,
    video_id: str,
    content: str,
    *,
    user_id: int,
) -> dict[str, Any]:
    """Append a user turn, run the LLM, persist the assistant turn,
    return both as a dict."""
    from app.repos import chat as chat_repo
    from app.repos import settings as settings_repo
    from app.services.chat import stream_reply

    video = await videos_repo.get(db, video_id)
    if video is None or video.transcript is None:
        raise ValueError("Video or transcript not found")
    settings = await settings_repo.get_all(db)
    model = settings.get("llm_model")
    if not model:
        raise ValueError("LLM not configured")
    api_key = settings.get("llm_api_key") or ""
    base_url = settings.get("llm_base_url")

    history = await chat_repo.history(db, video_id)
    await chat_repo.append(db, video_id, "user", content, user_id=user_id)

    collected: list[str] = []
    async for token in stream_reply(
        transcript=video.transcript,
        history=history,
        user_message=content,
        model=model,
        api_key=api_key,
        base_url=base_url,
    ):
        collected.append(token)
    answer = "".join(collected)
    await chat_repo.append(db, video_id, "assistant", answer, user_id=user_id)
    return {"answer": answer, "history_length": len(history) + 2}


async def list_playlists(
    db: aiosqlite.Connection, *, user_id: int
) -> list[dict[str, Any]]:
    playlists = await playlists_repo.list_for_user(db, user_id)
    out: list[dict[str, Any]] = []
    for p in playlists:
        videos = await playlists_repo.videos_for_playlist(db, p.id)
        out.append(
            {
                "id": p.id,
                "url": p.url,
                "title": p.title,
                "description": p.description,
                "video_count": len(videos),
                "last_refreshed_at": (
                    p.last_refreshed_at.isoformat()
                    if p.last_refreshed_at else None
                ),
                "created_at": p.created_at.isoformat(),
            }
        )
    return out


async def list_tags(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """
        SELECT t.name, COUNT(vt.video_id) AS n
        FROM tags t
        LEFT JOIN video_tags vt ON vt.tag_id = t.id
        GROUP BY t.id
        HAVING n > 0
        ORDER BY n DESC, t.name COLLATE NOCASE
        """
    )
    rows = await cursor.fetchall()
    return [{"name": row[0], "count": row[1]} for row in rows]


async def reindex_video(db: aiosqlite.Connection, video_id: str) -> None:
    if await videos_repo.get(db, video_id) is None:
        raise ValueError(f"Unknown video: {video_id}")
    await jobs_repo.enqueue(db, video_id)
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_services_api.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/services/api.py tests/test_services_api.py
git commit -m "feat(api): search, chat, playlists, tags helpers"
```

---

## Phase 5: REST API routes

### Task 5.1: api router scaffolding + health

**Files:**
- Create: `app/routes/api.py`
- Modify: `app/main.py`
- Create: `tests/test_routes_api_health.py`

- [ ] **Step 1: Failing test**

Create `tests/test_routes_api_health.py`:

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_api_health_open(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "version" in body
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/test_routes_api_health.py -v
```

Expected: 404.

- [ ] **Step 3: Create router scaffold**

Create `app/routes/api.py`:

```python
"""REST API. Mounted at /api/v1.

Each handler authenticates via the api dependency, then delegates to
services/api.py.
"""

from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import Config
from app.main import get_config, get_db
from app.services import api as api_svc
from app.services.auth import authenticate

router = APIRouter(prefix="/api/v1")

API_VERSION = "0.4.0"


async def current_user(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> int:
    return await authenticate(db, request)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": API_VERSION}
```

- [ ] **Step 4: Mount in `app/main.py`**

In `create_app()`, after the playlists router include, add:

```python
    from app.routes.api import router as api_router
    app.include_router(api_router)
```

- [ ] **Step 5: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_routes_api_health.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/api.py app/main.py tests/test_routes_api_health.py
git commit -m "feat(api): /api/v1 router scaffold + /health"
```

### Task 5.2: videos endpoints (submit, get, list, summary, transcript, reindex)

**Files:**
- Modify: `app/routes/api.py`
- Create: `tests/test_routes_api_videos.py`

- [ ] **Step 1: Failing tests**

Create `tests/test_routes_api_videos.py`:

```python
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.youtube import VideoMetadata


def _meta(vid: str = "apivid12345") -> VideoMetadata:
    return VideoMetadata(
        id=vid,
        url=f"https://youtu.be/{vid}",
        title="API Test Video",
        description="d",
        duration_seconds=120,
        thumbnail_url=None,
    )


def test_post_videos_async_returns_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        patch("app.services.api.fetch_metadata", AsyncMock(return_value=_meta())),
        patch("app.services.api.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/api/v1/videos",
            json={"url": "https://youtu.be/apivid12345"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["video_id"] == "apivid12345"
    assert body["summary_ready"] is False
    assert body["kind"] == "youtube"


def test_post_videos_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/v1/videos", json={"url": "ftp://x"})
    assert resp.status_code == 400


def test_get_video_returns_resource(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="getapi1", url="u", title="GotIt",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/getapi1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "getapi1"
    assert body["title"] == "GotIt"


def test_get_video_404(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/videos/nope")
    assert resp.status_code == 404


def test_list_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            for i in range(3):
                await videos_repo.upsert_metadata(
                    app.state.db, video_id=f"l{i}", url="u", title=f"T{i}",
                    description="", thumbnail_path=None, duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert "videos" in body
    assert len(body["videos"]) == 2


def test_get_summary_404_when_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="nosum", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/nosum/summary")
    assert resp.status_code == 404


def test_get_summary_returns_text_when_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="hassum", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "hassum", "## TL;DR\nyes", "model"
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/hassum/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert "TL;DR" in body["summary"]
    assert body["model"] == "model"


def test_reindex_returns_202(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="rev", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/api/v1/videos/rev/reindex")
    assert resp.status_code == 202
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Append to `app/routes/api.py`**

```python
from fastapi import Body, Query
from fastapi.responses import JSONResponse


@router.post("/videos")
async def api_submit_video(
    payload: dict = Body(...),
    wait: bool = Query(False),
    timeout: int = Query(60, ge=0, le=300),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    url = payload.get("url", "")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "url is required", "code": "INVALID_INPUT"},
        )
    try:
        resource = await api_svc.submit_video(
            db, config, url=url, user_id=user_id,
            wait=wait, timeout=timeout,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": str(e), "code": "IMPORT_FAILED"},
        ) from e
    status_code = 200 if resource["summary_ready"] else 202
    return JSONResponse(resource, status_code=status_code)


@router.get("/videos")
async def api_list_videos(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tag: str | None = Query(None),
    playlist_id: str | None = Query(None),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    videos = await api_svc.list_videos(
        db, limit=limit, offset=offset, tag=tag, playlist_id=playlist_id,
    )
    return {"videos": videos}


@router.get("/videos/{video_id}")
async def api_get_video(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    resource = await api_svc.get_video_resource(db, video_id)
    if resource is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    return resource


@router.get("/videos/{video_id}/summary")
async def api_get_summary(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    from app.repos import videos as videos_repo
    video = await videos_repo.get(db, video_id)
    if video is None or not video.summary:
        raise HTTPException(
            status_code=404,
            detail={"error": "Summary not available", "code": "NOT_FOUND"},
        )
    return {"summary": video.summary, "model": video.summary_model}


@router.get("/videos/{video_id}/transcript")
async def api_get_transcript(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    from app.repos import videos as videos_repo
    video = await videos_repo.get(db, video_id)
    if video is None or not video.transcript:
        raise HTTPException(
            status_code=404,
            detail={"error": "Transcript not available", "code": "NOT_FOUND"},
        )
    return {
        "transcript": video.transcript,
        "source": (
            video.transcript_source.value
            if video.transcript_source else None
        ),
    }


@router.post("/videos/{video_id}/reindex")
async def api_reindex_video(
    video_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    try:
        await api_svc.reindex_video(db, video_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        ) from None
    return JSONResponse({"queued": True}, status_code=202)


@router.post("/videos/{video_id}/chat")
async def api_chat(
    video_id: str,
    payload: dict = Body(...),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    content = payload.get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "content is required", "code": "INVALID_INPUT"},
        )
    try:
        result = await api_svc.chat_about_video(
            db, video_id, content, user_id=user_id,
        )
    except ValueError as e:
        msg = str(e)
        if "LLM" in msg:
            raise HTTPException(
                status_code=400,
                detail={"error": msg, "code": "LLM_NOT_CONFIGURED"},
            ) from e
        raise HTTPException(
            status_code=404,
            detail={"error": msg, "code": "NOT_FOUND"},
        ) from e
    return result
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_routes_api_videos.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/api.py tests/test_routes_api_videos.py
git commit -m "feat(api): videos endpoints (submit, get, list, summary, transcript, reindex, chat)"
```

### Task 5.3: search, playlists, tags endpoints

**Files:**
- Modify: `app/routes/api.py`
- Create: `tests/test_routes_api_search.py`
- Create: `tests/test_routes_api_playlists.py`
- Create: `tests/test_routes_api_tags.py`

- [ ] **Step 1: Failing tests for search**

Create `tests/test_routes_api_search.py`:

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_search_returns_hits(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="searchhit", url="u",
                title="Python tutorial",
                description="learn fastapi",
                thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/search?q=fastapi")
    assert resp.status_code == 200
    body = resp.json()
    assert "hits" in body
    assert any(h["id"] == "searchhit" for h in body["hits"])


def test_search_requires_query(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/search")
    assert resp.status_code in (400, 422)
```

Create `tests/test_routes_api_playlists.py`:

```python
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.playlist import PlaylistMetadata


def _meta(plid="PLapi"):
    return PlaylistMetadata(
        id=plid, url=f"https://youtube.com/playlist?list={plid}",
        title="API Playlist", description="", thumbnail_url=None,
        entries=[],
    )


def test_list_playlists_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/playlists")
    assert resp.status_code == 200
    assert resp.json()["playlists"] == []


def test_create_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    fake = _meta()
    with (
        patch("app.routes.api.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        patch("app.routes.api.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/api/v1/playlists",
            json={"url": "https://www.youtube.com/playlist?list=PLapi"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "PLapi"


def test_remove_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLrem", user_id=1,
                url="u", title="X", description="", thumbnail_path=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.delete("/api/v1/playlists/PLrem")
    assert resp.status_code == 200
    assert resp.json() == {"removed": True}
```

Create `tests/test_routes_api_tags.py`:

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_list_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import tags as tags_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vt1", url="u", title="vt1",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await tags_repo.set_tags_for_video(
                app.state.db, "vt1", ["one", "two"]
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/tags")
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["tags"]}
    assert names == {"one", "two"}
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Append to `app/routes/api.py`**

```python
from app.services.playlist import fetch_playlist
from app.services.playlist_sync import (
    load_older_videos as _load_older,
    sync_playlist as _sync_playlist,
)
from app.services.youtube import download_thumbnail
from app.repos import playlists as playlists_repo


@router.get("/search")
async def api_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    tag: str | None = Query(None),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    hits = await api_svc.search_videos(db, q, limit=limit, tag=tag)
    return {"hits": hits}


@router.get("/playlists")
async def api_list_playlists(
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    return {"playlists": await api_svc.list_playlists(db, user_id=user_id)}


@router.post("/playlists")
async def api_create_playlist(
    payload: dict = Body(...),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    url = payload.get("url", "")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "url is required", "code": "INVALID_INPUT"},
        )
    import re
    match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    if not match:
        raise HTTPException(
            status_code=400,
            detail={"error": "Not a playlist URL", "code": "INVALID_INPUT"},
        )

    cookies = config.cookies_path if config.cookies_path.exists() else None
    meta = await fetch_playlist(url, cookies_path=cookies)
    thumb_target = config.thumbnails_dir / f"playlist_{meta.id}.jpg"
    await download_thumbnail(meta.thumbnail_url, thumb_target)
    thumb_db_path = str(thumb_target) if thumb_target.exists() else None

    await playlists_repo.create(
        db,
        playlist_id=meta.id,
        user_id=user_id,
        url=meta.url,
        title=meta.title,
        description=meta.description,
        thumbnail_path=thumb_db_path,
    )
    await _sync_playlist(db, config, meta.id)
    rows = await api_svc.list_playlists(db, user_id=user_id)
    out = next((r for r in rows if r["id"] == meta.id), None)
    return JSONResponse(out, status_code=201)


@router.post("/playlists/{playlist_id}/refresh")
async def api_refresh_playlist(
    playlist_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    await _sync_playlist(db, config, playlist_id)
    return {"refreshed": True}


@router.post("/playlists/{playlist_id}/load-older")
async def api_load_older(
    playlist_id: str,
    count: int = Query(20, ge=1, le=100),
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    result = await _load_older(db, config, playlist_id, count=count)
    return {
        "newly_linked": result.newly_linked,
        "newly_enqueued": result.newly_enqueued,
    }


@router.delete("/playlists/{playlist_id}")
async def api_remove_playlist(
    playlist_id: str,
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    if await playlists_repo.get(db, playlist_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "code": "NOT_FOUND"},
        )
    await playlists_repo.delete(db, playlist_id)
    return {"removed": True}


@router.get("/tags")
async def api_list_tags(
    user_id: int = Depends(current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    return {"tags": await api_svc.list_tags(db)}
```

- [ ] **Step 4: Run, verify pass + lint + commit**

```bash
.venv/bin/pytest tests/test_routes_api_search.py tests/test_routes_api_playlists.py tests/test_routes_api_tags.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/api.py tests/test_routes_api_search.py tests/test_routes_api_playlists.py tests/test_routes_api_tags.py
git commit -m "feat(api): search, playlists, tags endpoints"
```

### Task 5.4: Auth enforcement on the API routes

**Files:**
- Modify: `tests/test_routes_api_videos.py`

The auth dependency is already on every handler via `current_user`.
We just need a test that proves it kicks in once a key is set.

- [ ] **Step 1: Append failing test to `tests/test_routes_api_videos.py`**

```python
def test_api_requires_key_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Generate a key
        client.post("/settings/api-key/generate", follow_redirects=False)
        # Without auth header, GET /api/v1/videos should 401
        resp = client.get("/api/v1/videos")
        assert resp.status_code == 401
        # Health stays open
        resp_h = client.get("/api/v1/health")
        assert resp_h.status_code == 200


def test_api_accepts_valid_bearer_after_key_set(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        gen = client.post("/settings/api-key/generate", follow_redirects=False)
        # Extract plaintext from the reveal page (yts_…)
        import re
        m = re.search(r"(yts_[a-z0-9]+)", gen.text)
        assert m is not None
        plaintext = m.group(1)
        resp = client.get(
            "/api/v1/videos",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 200
```

- [ ] **Step 2: Run, verify pass**

```bash
.venv/bin/pytest tests/test_routes_api_videos.py -v
```

If `/api/v1/health` is returning 401 (i.e. it has the dependency too):
remove the `Depends(current_user)` from the health route. Health should
be auth-free per spec.

- [ ] **Step 3: Run full suite + lint + commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add tests/test_routes_api_videos.py
git commit -m "test: API auth enforcement"
```

---

## Phase 6: MCP server

### Task 6.1: Add `mcp` dependency + register tools

**Files:**
- Modify: `pyproject.toml`
- Create: `app/routes/mcp.py`
- Modify: `app/main.py`
- Create: `tests/test_routes_mcp.py`

- [ ] **Step 1: Install MCP SDK**

```bash
.venv/bin/pip install -q "mcp[server]"
```

Verify:
```bash
.venv/bin/python -c "import mcp; print(mcp.__version__)"
```

- [ ] **Step 2: Add to pyproject.toml**

In `pyproject.toml`, append to `dependencies`:
```toml
    "mcp>=1.0",
```

- [ ] **Step 3: Failing tests**

Create `tests/test_routes_mcp.py`:

```python
"""MCP-tool dispatch tests.

We exercise the tool functions directly (not the SSE wire protocol) —
the SDK is responsible for serialization, but we want confidence the
yt-summary side returns sensible payloads.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.config import Config


@pytest.fixture
async def seeded_db_and_config(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    from app.repos import videos as videos_repo
    await videos_repo.upsert_metadata(
        db, video_id="mcptest1", url="https://youtu.be/mcptest1",
        title="MCP test", description="d",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_summary(
        db, "mcptest1", "## TL;DR\nyes", "model"
    )
    return db, config


async def test_mcp_get_summary(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_get_summary
    out = await _tool_get_summary(db, video_id="mcptest1")
    assert "TL;DR" in out


async def test_mcp_get_summary_unknown(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_get_summary
    with pytest.raises(ValueError):
        await _tool_get_summary(db, video_id="nope")


async def test_mcp_search(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_search
    hits = await _tool_search(db, query="MCP", limit=5)
    assert any(h["video_id"] == "mcptest1" for h in hits)


async def test_mcp_list_recent(seeded_db_and_config):
    db, _ = seeded_db_and_config
    from app.routes.mcp import _tool_list_recent
    rows = await _tool_list_recent(db, limit=10)
    assert any(r["video_id"] == "mcptest1" for r in rows)
```

- [ ] **Step 4: Run, verify failure**

- [ ] **Step 5: Implement `app/routes/mcp.py`**

```python
"""MCP server mounted at /mcp/sse.

Tools delegate to services/api.py. We expose a smaller surface than
the REST API on purpose — Claude does best with a focused toolset.
"""

import logging
from typing import Any

import aiosqlite
from fastapi import Request
from mcp.server.fastmcp import FastMCP

from app.config import Config
from app.services import api as api_svc

log = logging.getLogger(__name__)


# These tool implementations don't depend on FastMCP — they're plain
# async functions taking explicit db/config args. The MCP wrappers
# below pull db/config from request scope.

async def _tool_submit_url(
    db: aiosqlite.Connection,
    config: Config,
    url: str,
    *,
    user_id: int = 1,
    wait_for_summary: bool = False,
    timeout: int = 120,
) -> dict[str, Any]:
    resource = await api_svc.submit_video(
        db, config,
        url=url, user_id=user_id,
        wait=wait_for_summary, timeout=timeout,
    )
    out = {
        "video_id": resource["id"],
        "kind": resource["kind"],
        "summary_ready": resource["summary_ready"],
        "title": resource["title"],
    }
    if resource["summary_ready"]:
        from app.repos import videos as videos_repo
        v = await videos_repo.get(db, resource["id"])
        if v:
            out["summary"] = v.summary
    return out


async def _tool_search(
    db: aiosqlite.Connection,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    hits = await api_svc.search_videos(db, query, limit=limit)
    out: list[dict[str, Any]] = []
    for h in hits:
        excerpt = ""
        if h.get("summary_ready"):
            from app.repos import videos as videos_repo
            v = await videos_repo.get(db, h["id"])
            if v and v.summary:
                excerpt = v.summary[:200]
        out.append({
            "video_id": h["id"],
            "title": h["title"],
            "url": h["url"],
            "summary_excerpt": excerpt,
        })
    return out


async def _tool_get_summary(
    db: aiosqlite.Connection, video_id: str
) -> str:
    from app.repos import videos as videos_repo
    v = await videos_repo.get(db, video_id)
    if v is None or not v.summary:
        raise ValueError(f"No summary for {video_id}")
    return v.summary


async def _tool_get_transcript(
    db: aiosqlite.Connection, video_id: str
) -> str:
    from app.repos import videos as videos_repo
    v = await videos_repo.get(db, video_id)
    if v is None or not v.transcript:
        raise ValueError(f"No transcript for {video_id}")
    return v.transcript


async def _tool_ask_video(
    db: aiosqlite.Connection,
    video_id: str,
    question: str,
    *,
    user_id: int = 1,
) -> str:
    result = await api_svc.chat_about_video(
        db, video_id, question, user_id=user_id
    )
    return result["answer"]


async def _tool_list_recent(
    db: aiosqlite.Connection,
    limit: int = 20,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    videos = await api_svc.list_videos(db, limit=limit, tag=tag)
    return [
        {
            "video_id": v["id"],
            "title": v["title"],
            "url": v["url"],
            "summary_ready": v["summary_ready"],
        }
        for v in videos
    ]


def build_mcp_server(app_state) -> FastMCP:
    """Wire the tool functions into FastMCP, threading the FastAPI
    app.state.db / app.state.config through."""
    mcp = FastMCP("yt-summary")

    @mcp.tool()
    async def submit_url(
        url: str,
        wait_for_summary: bool = False,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """Submit a YouTube or article URL and start processing.

        With wait_for_summary=True, the call blocks up to `timeout`
        seconds and returns the summary inline if ready.
        """
        return await _tool_submit_url(
            app_state.db, app_state.config, url,
            wait_for_summary=wait_for_summary, timeout=timeout,
        )

    @mcp.tool()
    async def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the library by keyword and meaning. Returns top hits."""
        return await _tool_search(app_state.db, query, limit=limit)

    @mcp.tool()
    async def get_summary(video_id: str) -> str:
        """Return the full Markdown summary for a video."""
        return await _tool_get_summary(app_state.db, video_id)

    @mcp.tool()
    async def get_transcript(video_id: str) -> str:
        """Return the full transcript / article body."""
        return await _tool_get_transcript(app_state.db, video_id)

    @mcp.tool()
    async def ask_video(video_id: str, question: str) -> str:
        """Ask a question about a video's content. Synchronous; persists
        the question + answer into the video's chat history."""
        return await _tool_ask_video(app_state.db, video_id, question)

    @mcp.tool()
    async def list_recent(
        limit: int = 20,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent videos in the library."""
        return await _tool_list_recent(app_state.db, limit=limit, tag=tag)

    return mcp
```

- [ ] **Step 6: Mount MCP in `app/main.py`**

In `create_app()`, after the api router include, add:

```python
    from app.routes.mcp import build_mcp_server
    mcp_server = build_mcp_server(app.state)
    app.mount("/mcp", mcp_server.sse_app())
```

NOTE: FastMCP's `sse_app()` returns a Starlette app. We mount it under
`/mcp` so the actual SSE endpoint is `/mcp/sse`. Verify FastMCP's
default route name with the installed version; if the route is
`/messages` instead of `/sse`, adjust the README accordingly.

- [ ] **Step 7: Run + lint + commit**

```bash
.venv/bin/pytest tests/test_routes_mcp.py -v
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/routes/mcp.py app/main.py pyproject.toml tests/test_routes_mcp.py
git commit -m "feat(mcp): MCP server with submit/search/get/ask/list tools"
```

---

## Phase 7: Boot warning + README

### Task 7.1: Boot warning when no API key is configured

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add warning in lifespan**

In `app/main.py`, inside the `lifespan` function, after `init_schema(db)`
and before the worker is started, add:

```python
    # Warn loudly if no API key is set — anyone on the LAN can call
    # the API. Useful default for first run, but the user should
    # generate one before exposing the box.
    from app.repos import users as _users_repo
    _user = await _users_repo.get_default_user(db)
    if _user is None or _user.api_key_hash is None:
        logging.getLogger("yt_summary.boot").warning(
            "No API key configured — /api/v1 and /mcp/sse are open to "
            "anyone on the LAN. Generate one at /settings."
        )
```

Add `import logging` to the top of `app/main.py` if not already there.

- [ ] **Step 2: Run full suite to make sure nothing breaks + commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
git add app/main.py
git commit -m "feat(boot): warn when API key is unset"
```

### Task 7.2: README updates

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Append section**

Add after the existing "Development" section:

```markdown
## Programmatic access

Once the app is running, generate an API key in Settings (`/settings`,
"API access" section). The same key gates both surfaces:

### REST API

OpenAPI docs: `http://localhost:8200/api/v1/docs`

Quick example:
```bash
curl -X POST http://localhost:8200/api/v1/videos \
  -H "Authorization: Bearer yts_..." \
  -H "Content-Type: application/json" \
  -d '{"url":"https://youtu.be/dQw4w9WgXcQ"}'
```

### MCP server

Endpoint: `http://localhost:8200/mcp/sse`

For Claude Desktop, add to your MCP config:
```json
{
  "mcpServers": {
    "yt-summary": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://your-host:8200/mcp/sse",
        "--header", "Authorization: Bearer yts_..."
      ]
    }
  }
}
```

Claude Code (CLI) and other MCP-over-HTTP-capable hosts can connect
directly without `mcp-remote`.

The server exposes these tools: `submit_url`, `search`, `get_summary`,
`get_transcript`, `ask_video`, `list_recent`.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: API + MCP usage in README"
```

---

## Phase 8: Final smoke + tag

### Task 8.1: Manual smoke test

- [ ] **Step 1: Run full suite + lint**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```

Expected: all tests pass, lint clean.

- [ ] **Step 2: Boot the app**

```bash
YTS_DATA_DIR=./data .venv/bin/uvicorn app.main:app
```

In a browser:
1. Open `http://localhost:8000/settings` and generate an API key
2. Copy it
3. `curl http://localhost:8000/api/v1/health` (no auth needed)
4. `curl http://localhost:8000/api/v1/videos` without auth → 401
5. `curl -H "Authorization: Bearer yts_..." http://localhost:8000/api/v1/videos` → 200
6. Open `http://localhost:8000/api/v1/docs` → Swagger UI lists all endpoints

- [ ] **Step 3: Push**

```bash
git push origin main
```

CI runs, GHCR builds the new `:latest` image automatically (the workflow
triggers on every main push).

- [ ] **Step 4: Tag the release**

```bash
git tag v0.4.0
git push origin v0.4.0
```

---

## Self-Review

**Spec coverage:**
- Users table + default user → Task 1.1, 1.2
- users repo → Task 1.3
- Auth service (key gen, hashing, dependency) → Tasks 2.1, 2.2
- Settings UI for key → Task 3.1
- Shared service layer → Tasks 4.1, 4.2
- REST endpoints (videos, search, playlists, tags, health) → Tasks 5.1-5.4
- MCP server with the spec'd tools → Task 6.1
- Boot warning → Task 7.1
- README → Task 7.2
- Smoke + tag → Task 8.1

**Placeholder scan:** No "TBD"/"add appropriate error handling"/"similar to". Every step has runnable code or a runnable command.

**Type consistency:** `VideoResource` TypedDict defined in Task 4.1, used in 4.2 + every API route. `current_user` dependency named consistently. `_tool_*` MCP helpers all take `db` as first arg.

One thing I noticed during review: the `_TS` and local imports inside `services/api.py::submit_video` are slightly awkward (avoiding circular imports). Worth cleaning up later but functional. Plan keeps them as-is.

No spec gaps detected.
