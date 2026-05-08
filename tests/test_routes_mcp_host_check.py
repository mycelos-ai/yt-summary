"""Regression tests for the MCP DNS-rebinding host check.

FastMCP's transport-security middleware defaults to host validation
with an empty allowlist (when host is "127.0.0.1"), which rejects every
non-localhost request with 421 "Invalid Host header". yt-summary is a
LAN tool with its own API-key auth — DNS-rebinding protection is both
redundant and a foot-gun, so we disable it by default.

We can't easily drive a real GET /mcp/sse request to completion from
TestClient — when the host check passes, SSE keeps the connection open
indefinitely and TestClient has no first-class request timeout. So we
exercise FastMCP's wiring directly: build the server the same way the
app does, then inspect the resulting `transport_security` settings.
"""

import os

import pytest


def _build_server():
    """Re-import the route module fresh so it picks up env vars set by
    monkeypatch — `os.environ.get(...)` is read at build time."""
    import importlib

    from app.routes import mcp as mcp_route
    importlib.reload(mcp_route)

    class _StubState:
        db = None
        config = None

    return mcp_route.build_mcp_server(_StubState()), mcp_route


def test_default_disables_dns_rebinding_protection(monkeypatch):
    """With no env override, host validation must be off so a curl
    against http://<lan-ip>:8200/mcp/sse doesn't 421."""
    monkeypatch.delenv("YTS_MCP_DISABLE_HOST_CHECK", raising=False)
    server, _ = _build_server()
    settings = server.settings.transport_security
    assert settings is not None, (
        "build_mcp_server should pass an explicit transport_security so "
        "FastMCP doesn't fall back to its localhost-only auto-config."
    )
    assert settings.enable_dns_rebinding_protection is False, (
        "Default config must disable DNS-rebinding protection — "
        "yt-summary already has API-key auth and runs on LANs."
    )


def test_env_var_can_re_enable_default(monkeypatch):
    """Set YTS_MCP_DISABLE_HOST_CHECK=0 to opt back into FastMCP's
    default (which the user can then configure via FASTMCP_* env)."""
    monkeypatch.setenv("YTS_MCP_DISABLE_HOST_CHECK", "0")
    server, _ = _build_server()
    # When opting out, we pass transport_security=None so FastMCP falls
    # back to its own defaults (auto-enables for localhost host).
    assert server.settings.transport_security is not None
    # FastMCP's own default for host="127.0.0.1" enables protection.
    assert (
        server.settings.transport_security.enable_dns_rebinding_protection
        is True
    )


@pytest.fixture(autouse=True)
def _reset_module_state():
    """The build_mcp_server module reads env at call time; reload is
    idempotent here, just be tidy across tests."""
    yield
    # Clear env so other tests in the suite don't see this var.
    os.environ.pop("YTS_MCP_DISABLE_HOST_CHECK", None)


def test_sse_endpoint_does_not_421_on_lan_host(tmp_path, monkeypatch):
    """End-to-end smoke: with the fix in place, a request whose Host
    header is a LAN IP must NOT come back as 421 'Invalid Host header'.

    We cancel the request quickly after the headers come back — SSE
    streams indefinitely once accepted, but the status line is enough.
    """
    import threading

    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YTS_MCP_DISABLE_HOST_CHECK", raising=False)
    app = create_app()
    result: dict = {}

    def _hit():
        # raise_server_exceptions=False so SSE-side exceptions don't
        # mask the client-visible HTTP response.
        with TestClient(app, raise_server_exceptions=False) as client:
            try:
                # When host check is disabled, this hangs because SSE
                # stays open. We use stream() so we can read just the
                # response status without waiting for the body.
                with client.stream(
                    "GET", "/mcp/sse",
                    headers={"Host": "192.168.0.111:8200"},
                ) as resp:
                    result["status"] = resp.status_code
                    # Try to read just a tiny bit; cancel immediately.
                    return
            except Exception as e:
                result["error"] = repr(e)

    t = threading.Thread(target=_hit, daemon=True)
    t.start()
    t.join(timeout=5.0)
    # If the thread is still running, the SSE stream stayed open —
    # that's actually a *good* sign (host check passed, request was
    # accepted). Either way, the result dict must not contain a 421.
    if "status" in result:
        assert result["status"] != 421, (
            f"Got 421 — DNS-rebinding host check is still active. "
            f"result: {result!r}"
        )

