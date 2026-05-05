# yt-summary

Self-hosted web UI that summarizes YouTube videos. Runs in Docker on Mac, Linux, and Raspberry Pi (ARM64).

## Quick start

```bash
docker compose -f docker/docker-compose.yml up -d
```

Open http://localhost:8000 and configure your LLM provider in Settings.

## Development

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload
pytest
```

See [design spec](docs/superpowers/specs/2026-05-05-yt-summary-design.md) for architecture.
