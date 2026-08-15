"""The MCP server speaks stateless Streamable HTTP at POST /mcp.

Stateless means every request stands alone: no session id to carry, no
long-lived event stream to keep open, nothing to resume after a
reconnect. That is honest for this server — all tools are plain
request/response over the DB, and `export_since` paginates through a
cursor in the payload rather than through server-side state.

The transport moved here from SSE, which needed a persistent connection
plus a session id on every message.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A live app on an isolated data dir.

    create_app() reads YTS_DATA_DIR at startup; without this the
    lifespan tries to create the real /data. Same pattern the other
    route tests use.
    """
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    # TestClient sends Host: testserver, which the DNS-rebinding check
    # rejects (a fresh test DB has no API key, so the check stays on by
    # design). That guard is covered by test_routes_mcp_host_check.py;
    # here we are testing the transport, so switch it off.
    monkeypatch.setenv("YTS_MCP_DISABLE_HOST_CHECK", "1")
    with TestClient(create_app()) as c:
        yield c


class _State:
    """Minimal stand-in for app.state — build_mcp_server only reads
    `config` at build time (for the API-key host-check probe)."""

    db = None
    config = None


def test_mcp_server_is_configured_stateless():
    from app.routes.mcp import build_mcp_server

    server = build_mcp_server(_State())
    assert server.settings.stateless_http is True


def test_streamable_path_is_root_so_it_lands_on_mcp():
    """The app is mounted at /mcp; FastMCP's own default path is also
    "/mcp", which would nest the endpoint at /mcp/mcp."""
    from app.routes.mcp import build_mcp_server

    server = build_mcp_server(_State())
    assert server.settings.streamable_http_path == "/"


def _initialize_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        },
    }


def test_mcp_endpoint_answers_without_a_session_id(client):
    """A bare POST — no session id anywhere — must be served.

    Under the old SSE transport this was impossible: the client first
    had to open the event stream to be issued a session id.
    """
    r = client.post(
        "/mcp",
        json=_initialize_request(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 200, r.text
    # The server answered the JSON-RPC call itself, rather than
    # rejecting the request for a missing/unknown session.
    assert "yt-summary" in r.text


def test_two_requests_share_no_session(client):
    """Two independent POSTs both succeed — neither depends on the
    other having happened."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    first = client.post("/mcp", json=_initialize_request(), headers=headers)
    second = client.post("/mcp", json=_initialize_request(), headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text


def test_sse_endpoint_is_gone(client):
    """SSE was removed, not kept in parallel — clients must be
    reconfigured to the new endpoint."""
    assert client.get("/mcp/sse").status_code == 404
