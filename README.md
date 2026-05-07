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

## Whisper backend options

When a video has no usable YouTube subtitles, yt-summary falls back to
Whisper. By default this runs locally inside the container with
`faster-whisper` on CPU — fine on a Mac/PC, but slow on a Raspberry Pi
(roughly 1× realtime with `small`, so a 1h video takes ~1h).

You can route Whisper to any host that speaks the OpenAI
`/v1/audio/transcriptions` contract. Set **Whisper Base URL** (and
**Whisper API Key** if needed) in Settings.

### 1. Local in container (default)

Empty Whisper Base URL. Whisper Model is `tiny` / `base` / `small` /
`medium` / `large-v3`. Drop to `base` if `small` is too slow on your
hardware — quality dip is small once the LLM paraphrases.

### 2. Self-hosted faster-whisper-server (Docker, e.g. on a Mac mini)

```bash
docker run -d --name fw-server -p 8000:8000 \
  -v ~/.cache/whisper:/root/.cache/huggingface \
  fedirz/faster-whisper-server:latest-cpu
```

Then in yt-summary Settings:
- Whisper Base URL: `http://mac-mini.local:8000/v1`
- Whisper API Key: *(leave blank)*
- Whisper Model: `Systran/faster-whisper-large-v3` (or whatever the
  server's model list shows)

### 3. Groq Cloud

Fastest option. `whisper-large-v3` at ~150× realtime, ~$0.04 per hour
of audio.

- Whisper Base URL: `https://api.groq.com/openai/v1`
- Whisper API Key: `gsk_...`
- Whisper Model: `whisper-large-v3`

### 4. OpenAI Cloud

- Whisper Base URL: `https://api.openai.com/v1`
- Whisper API Key: `sk-...`
- Whisper Model: `whisper-1`

> Switching backends doesn't require an app restart — change the
> Settings, hit Save, then **Re-summarize** the video.

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
