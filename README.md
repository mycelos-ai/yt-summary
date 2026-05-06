# yt-summary

Self-hosted web UI that summarizes YouTube videos. Runs in Docker on Mac, Linux, and Raspberry Pi (ARM64).

## Quick start

```bash
docker compose -f docker/docker-compose.yml up -d
```

Open http://localhost:8200 and configure your LLM provider in Settings.
(The compose file maps host port `8200` to the container's internal `8000`.)

## Development

```bash
pip install -e ".[dev]"
YTS_DATA_DIR=./data uvicorn app.main:app --reload
pytest
```

When running locally without Docker, uvicorn defaults to port 8000.

See the [core design spec](docs/superpowers/specs/2026-05-05-yt-summary-design.md)
and the [playlists spec](docs/superpowers/specs/2026-05-06-playlists-design.md)
for architecture.

## Programmatic access

Once the app is running, generate an API key in Settings (`/settings`,
"API access" section). The same key gates both surfaces:

### REST API

OpenAPI docs: `http://localhost:8200/docs` (Swagger UI for the whole app, including `/api/v1/*`)

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
