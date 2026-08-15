# Stateless Streamable HTTP for the MCP server — Implementation Plan

**Goal:** Replace the stateful SSE transport with stateless Streamable HTTP, so every MCP request stands alone and no session state has to survive between calls.

**Architecture:** `FastMCP` already supports this in the installed 1.26 — no upgrade to `mcp` 2.0 is involved. Three coupled changes: set `stateless_http=True` and `streamable_http_path="/"` on the `FastMCP` constructor, mount `streamable_http_app()` instead of `sse_app()`, and run the session manager inside the app's existing lifespan (a mounted sub-app's own lifespan never runs).

**Decisions taken (2026-08-15):** SSE is removed, not kept in parallel. `YTS_MCP_DISABLE_HOST_CHECK` stays in `docker-compose.yml` for now — the host check is transport-independent, so stateless does not automatically make it unnecessary; that gets re-tested behind Traefik separately.

## Why stateless, and why no shim

All ten MCP tools are plain request/response functions over `app_state.db`. None holds a session, none depends on a previous call. `export_since` paginates through a cursor carried in the payload, deliberately, rather than through server-side state. The state lives in SQLite, not in the MCP session — so there is nothing for a stateful shim to preserve. A shim would guard state that does not exist.

## Three findings that shape the work

These were verified against the installed library, not assumed.

**1. `stateless_http` is a constructor setting, not an argument.**
`FastMCP.streamable_http_app()` takes no parameters — it reads `self.settings.stateless_http` and `self.settings.transport_security`. The switch therefore belongs in the `FastMCP(...)` call inside `build_mcp_server`.

**2. A mounted sub-app's lifespan does NOT run.** Verified:

```
sub-lifespan lief NICHT
```

`streamable_http_app()` sets up its `StreamableHTTPSessionManager` via its own Starlette `lifespan=lambda app: self.session_manager.run()`. Starlette does not run the lifespan of an app attached with `app.mount(...)`, so that manager would never start and the endpoint would fail at the first request. The manager must run inside the existing FastAPI lifespan in `app/main.py:26` instead. This is also why the current code uses `sse_app()` — SSE needs no such manager.

`session_manager.run()` is single-use: "This method can only be called once per instance." The test suite calls `create_app()` repeatedly, so the manager must be created per app instance, not at module level.

**3. The default path collides with the mount point.**
`settings.streamable_http_path` defaults to `/mcp`. Mounting that app at `/mcp` yields `/mcp/mcp`. Set `streamable_http_path="/"` so the endpoint lands on `/mcp`.

## Breaking change

The endpoint moves from `GET /mcp/sse` (plus `POST /mcp/messages/?session_id=…`) to a single `POST /mcp`. Every client configuration must be updated. Seven places in this repo name the old path and must move with it:

| File | What it is |
|---|---|
| `app/routes/mcp.py:1` | module docstring |
| `app/main.py:95` | startup warning about an unprotected surface |
| `app/templates/api_key_reveal.html:116,135` | the copy-paste client config shown to the user |
| `app/templates/settings.html:603,635` | Settings card text |
| `README.md:261,271` | endpoint docs and example config |

## Tasks

### Task 1: Stateless transport

**Files:**
- Modify: `app/routes/mcp.py` (the `FastMCP(...)` call in `build_mcp_server`, ~line 270; module docstring line 1)
- Modify: `app/main.py` (lifespan ~line 26; mount at line 308; warning text line 95)
- Test: `tests/test_routes_mcp_transport.py` (create)

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
"""The MCP server speaks stateless Streamable HTTP at POST /mcp.

Stateless means each request stands alone: no session id, no
long-lived event stream, nothing to resume. All tools are plain
request/response over the DB, so there is no state to preserve.
"""


def test_mcp_server_is_configured_stateless():
    from app.routes.mcp import build_mcp_server

    class _State:
        db = None
        config = None

    server = build_mcp_server(_State())
    assert server.settings.stateless_http is True
    # Mounted at /mcp, so the app's own path must be root or the
    # endpoint ends up at /mcp/mcp.
    assert server.settings.streamable_http_path == "/"


def test_mcp_endpoint_answers_without_a_session_id():
    """A bare POST /mcp must be handled, not rejected for a missing session."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        r = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    # The point is that the transport handled it — not a 404 (wrong
    # path) and not a 400 about a missing session id.
    assert r.status_code == 200, r.text
    assert "session" not in r.text.lower() or r.status_code == 200


def test_sse_endpoint_is_gone():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/mcp/sse").status_code == 404
```

- [ ] **Step 2: Run them, confirm they fail**

`python -m pytest tests/test_routes_mcp_transport.py -q` — expect failures on `stateless_http` and on `POST /mcp` 404-ing.

- [ ] **Step 3: Configure the server**

In `app/routes/mcp.py`, in `build_mcp_server`, extend the existing `FastMCP(...)` call:

```python
    mcp = FastMCP(
        "yt-summary",
        transport_security=transport_security,
        # Stateless: every request carries everything it needs. All
        # tools are request/response over the DB, and export_since
        # paginates via a cursor in the payload, so no session state
        # exists to preserve across calls.
        stateless_http=True,
        # This app is mounted at /mcp; without "/" the endpoint would
        # land at /mcp/mcp.
        streamable_http_path="/",
    )
```

Update the module docstring's first line from `"""MCP server mounted at /mcp/sse.` to `"""MCP server mounted at /mcp (stateless Streamable HTTP).`

- [ ] **Step 4: Run the session manager in the app lifespan**

In `app/main.py`, the mounted sub-app's lifespan will not run, so start the manager in the existing one. Build the server once in `create_app`, stash it on `app.state`, and enter its session manager in `lifespan`:

In `create_app` (replacing lines 306-308):

```python
    from app.routes.mcp import build_mcp_server
    mcp_server = build_mcp_server(app.state)
    app.state.mcp_server = mcp_server
    app.mount("/mcp", mcp_server.streamable_http_app())
```

In `lifespan`, inside the existing `async with` / startup section, before yielding:

```python
    # streamable_http_app() wires its session manager into ITS OWN
    # Starlette lifespan, and Starlette does not run the lifespan of a
    # mounted sub-app. So run it here or the endpoint dies on the first
    # request. run() is single-use per instance, which is why the
    # server is built per create_app() rather than at module level.
    mcp_server = getattr(app.state, "mcp_server", None)
```

Enter `mcp_server.session_manager.run()` as an async context manager for the lifetime of the app, alongside the other startup work, and let it exit on shutdown. Match the file's existing structure for how other long-lived tasks are started and stopped.

- [ ] **Step 5: Run the tests, confirm they pass**

- [ ] **Step 6: Full suite + lint**

`python -m pytest tests/ -q` and `python -m ruff check app/ tests/`. The host-check tests in `tests/test_routes_mcp_host_check.py` inspect `transport_security` on the built server and must still pass — `transport_security` is unchanged by this task.

- [ ] **Step 7: Commit**

```bash
git add app/routes/mcp.py app/main.py tests/test_routes_mcp_transport.py
git commit -m "feat(mcp): serve stateless Streamable HTTP at /mcp instead of SSE"
```

### Task 2: Update every reference to the old path

**Files:** `app/main.py:95`, `app/templates/api_key_reveal.html:116,135`, `app/templates/settings.html:603,635`, `README.md:261,271`

- [ ] **Step 1:** Replace `/mcp/sse` with `/mcp` in each location listed in the table above. In `api_key_reveal.html` and `README.md` these are client configuration snippets a user copies — check the surrounding JSON/CLI shape still makes sense for a plain POST endpoint, not just the URL string.
- [ ] **Step 2:** `grep -rn "mcp/sse" app/ README.md docker/` must return nothing.
- [ ] **Step 3:** `python -m pytest tests/ -q` (template rendering is covered by route tests) and `python -m ruff check app/ tests/`.
- [ ] **Step 4:** Commit.

## Verification

- [ ] Full suite green, ruff clean.
- [ ] `docker compose up -d --build`, then a real MCP client configured against `http://<host>:8200/mcp` can list tools and call `export_since`.
- [ ] The old `/mcp/sse` returns 404 — confirming clients must be reconfigured.

## Client reconfiguration (after deploy)

Every MCP client pointing at this server must change from `http://<host>:8200/mcp/sse` to `http://<host>:8200/mcp`, and from an SSE-type server entry to a Streamable HTTP one. This includes the connection used from Claude Code. Until reconfigured, those clients will fail to connect.

## Out of scope

- Migrating to `mcp` 2.0 (`FastMCP` → `MCPServer`). Pinned at `<2` in `pyproject.toml`; not needed for stateless.
- Removing `YTS_MCP_DISABLE_HOST_CHECK`. The host check validates the Host header and is independent of transport; whether it can go is a separate question to settle behind the real proxy.
- Any change to tool behavior or signatures.
