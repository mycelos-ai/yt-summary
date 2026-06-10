"""Tests for the MCP DNS-rebinding host check.

FastMCP's transport-security middleware validates the Host header. For a
LAN tool with its own API-key auth that protection is redundant — but
only *once a key exists*. The dangerous compose is the out-of-the-box
state: no API key (auth disabled) AND host-check disabled means a
malicious website can DNS-rebind to http://<lan-host>:8200 and drive the
MCP surface from a victim's browser.

So the default is coupled to API-key presence:
  * no key configured  -> host-check ENABLED (protection on)
  * key configured     -> host-check relaxed (protection off; the key
                          gates every request anyway)
The explicit env var YTS_MCP_DISABLE_HOST_CHECK overrides both ways.

We can't easily drive a real GET /mcp/sse request to completion from
TestClient — when the host check passes, SSE keeps the connection open
indefinitely. So we exercise FastMCP's wiring directly: build the server
the same way the app does, then inspect the resulting
`transport_security` settings.
"""

import os

import pytest


def _build_server(config=None):
    """Re-import the route module fresh so it picks up env vars set by
    monkeypatch — `os.environ.get(...)` is read at build time."""
    import importlib

    from app.routes import mcp as mcp_route
    importlib.reload(mcp_route)

    class _StubState:
        db = None
        config = None

    state = _StubState()
    state.config = config
    return mcp_route.build_mcp_server(state), mcp_route


def _config_with_key(tmp_path, *, with_key: bool):
    """Create a Config whose app.db has the schema and, optionally, a
    default user with an api_key_hash set."""
    import asyncio

    from app.config import Config
    from app.db import connect, init_schema

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    async def setup():
        db = await connect(cfg)
        await init_schema(db)
        if with_key:
            from app.repos import users as users_repo
            from app.services.auth import hash_api_key
            await users_repo.set_api_key(
                db, user_id=1,
                key_hash=hash_api_key("yts_present"),
                key_prefix="yts_pres",
            )
        await db.close()

    asyncio.get_event_loop().run_until_complete(setup())
    return cfg


def test_default_enables_protection_when_no_api_key(tmp_path, monkeypatch):
    """Out of the box (no key, no env override) host-check must be ON —
    otherwise a DNS-rebind from a victim's browser can drive the open
    MCP surface."""
    monkeypatch.delenv("YTS_MCP_DISABLE_HOST_CHECK", raising=False)
    cfg = _config_with_key(tmp_path, with_key=False)
    server, _ = _build_server(cfg)
    settings = server.settings.transport_security
    assert settings is None or (
        settings.enable_dns_rebinding_protection is True
    ), "No API key configured → protection must stay enabled."


def test_default_disables_protection_when_api_key_configured(tmp_path, monkeypatch):
    """Once an API key exists, the key gates every request, so the
    redundant host-check is relaxed by default."""
    monkeypatch.delenv("YTS_MCP_DISABLE_HOST_CHECK", raising=False)
    cfg = _config_with_key(tmp_path, with_key=True)
    server, _ = _build_server(cfg)
    settings = server.settings.transport_security
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is False, (
        "API key configured → host-check relaxed so LAN clients don't 421."
    )


def test_env_var_1_forces_disable_even_without_key(tmp_path, monkeypatch):
    """Explicit YTS_MCP_DISABLE_HOST_CHECK=1 disables protection
    regardless of key presence."""
    monkeypatch.setenv("YTS_MCP_DISABLE_HOST_CHECK", "1")
    cfg = _config_with_key(tmp_path, with_key=False)
    server, _ = _build_server(cfg)
    settings = server.settings.transport_security
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is False


def test_env_var_0_forces_enable_even_with_key(tmp_path, monkeypatch):
    """Explicit YTS_MCP_DISABLE_HOST_CHECK=0 keeps protection on even
    when a key is configured (user opts back into FastMCP's default)."""
    monkeypatch.setenv("YTS_MCP_DISABLE_HOST_CHECK", "0")
    cfg = _config_with_key(tmp_path, with_key=True)
    server, _ = _build_server(cfg)
    settings = server.settings.transport_security
    assert settings is None or (
        settings.enable_dns_rebinding_protection is True
    )


@pytest.fixture(autouse=True)
def _reset_module_state():
    yield
    os.environ.pop("YTS_MCP_DISABLE_HOST_CHECK", None)
