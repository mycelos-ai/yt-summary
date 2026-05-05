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

See [design spec](docs/superpowers/specs/2026-05-05-yt-summary-design.md) for architecture.
