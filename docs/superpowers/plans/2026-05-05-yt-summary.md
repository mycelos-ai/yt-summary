# yt-summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Self-hosted Docker web app that turns a YouTube URL into a written summary using yt-dlp + faster-whisper fallback + LiteLLM, with per-video chat and Markdown permalinks.

**Architecture:** Single FastAPI process. Server-rendered Jinja2 templates with HTMX for interactivity and SSE for streaming. SQLite holds videos, jobs, chat messages, and settings. An asyncio worker started at app boot polls the jobs table FIFO and runs the pipeline (yt-dlp → captions or Whisper → LiteLLM). One Docker image, multi-arch (amd64+arm64). All host state lives under `/data` (bind-mounted).

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Jinja2, HTMX, Alpine.js, aiosqlite, yt-dlp, faster-whisper, LiteLLM, pytest, ruff, pyright, Docker Buildx, GitHub Actions.

**Reference:** [docs/superpowers/specs/2026-05-05-yt-summary-design.md](../specs/2026-05-05-yt-summary-design.md)

---

## File Structure

Production code lives under `app/`:

```
app/
  __init__.py
  main.py                # FastAPI app factory + lifespan (starts worker)
  config.py              # paths, env vars, default settings
  db.py                  # aiosqlite connection + schema migrations
  models.py              # dataclasses for Video, Job, ChatMessage, Settings
  repos/
    __init__.py
    videos.py            # CRUD for videos table
    jobs.py              # job queue operations (enqueue, claim, complete, fail)
    chat.py              # CRUD for chat_messages
    settings.py          # key-value get/set
  services/
    __init__.py
    youtube.py           # yt-dlp wrappers (metadata, subs, audio download)
    whisper.py           # faster-whisper transcription
    transcript.py        # orchestrates subs > auto-subs > whisper
    summarizer.py        # LiteLLM single-shot + map-reduce
    chat.py              # LiteLLM streaming chat with transcript context
    curl_parser.py       # parse curl command → cookies.txt
  worker.py              # asyncio task: poll jobs, run pipeline
  routes/
    __init__.py
    home.py              # GET / (list + search)
    videos.py            # POST /videos, GET /v/{id}, GET /v/{id}.md, GET /v/{id}/status
    chat.py              # POST /v/{id}/chat (SSE)
    settings.py          # GET/POST /settings, POST /settings/youtube-curl
  templates/
    base.html
    home.html
    video_card.html      # HTMX fragment
    video_detail.html
    video_status.html    # HTMX fragment polled every 2s
    settings.html
    _chat_message.html   # HTMX fragment per chat message
  static/
    htmx.min.js
    alpine.min.js
    app.css

tests/
  conftest.py            # pytest fixtures (tmp DB, test client)
  test_db.py
  test_repos_videos.py
  test_repos_jobs.py
  test_repos_chat.py
  test_repos_settings.py
  test_services_curl_parser.py
  test_services_youtube.py    # uses recorded yt-dlp JSON fixtures
  test_services_transcript.py
  test_services_summarizer.py # mocks LiteLLM
  test_services_chat.py
  test_worker.py
  test_routes_home.py
  test_routes_videos.py
  test_routes_chat.py
  test_routes_settings.py
  fixtures/
    yt_dlp_metadata.json
    yt_dlp_subs.vtt
    curl_youtube.txt

docker/
  Dockerfile
  docker-compose.yml
  entrypoint.sh

.github/workflows/
  ci.yml
  release.yml

pyproject.toml
README.md
.gitignore
.dockerignore
```

**File responsibilities:**
- `main.py` is the composition root: builds the app, mounts routes, starts the worker on lifespan.
- `db.py` owns the schema. Repos call into it but never define schema themselves.
- `repos/*` are thin async functions over `aiosqlite.Connection`. No business logic.
- `services/*` are pure-ish functions; they take inputs (URL, transcript, etc.) and return values. They don't touch the DB directly — the worker glues services and repos together.
- `worker.py` is the only place where services and repos meet for write paths.
- `routes/*` are FastAPI routers. They call repos for reads and `worker.enqueue()` for writes.

---

## Phase 1: Skeleton

Goal: a working FastAPI app with one route, tests passing, and a clean repo structure to build on.

### Task 1.1: Project scaffolding (pyproject, gitignore, dockerignore)

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.dockerignore`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "yt-summary"
version = "0.1.0"
description = "Self-hosted YouTube summarization web app"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "jinja2>=3.1",
    "python-multipart>=0.0.12",
    "aiosqlite>=0.20",
    "yt-dlp>=2024.12.0",
    "faster-whisper>=1.1.0",
    "litellm>=1.55",
    "httpx>=0.28",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.25",
    "pytest-cov>=6.0",
    "ruff>=0.8",
    "pyright>=1.1.390",
    "respx>=0.22",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "ASYNC", "SIM"]

[tool.pyright]
include = ["app", "tests"]
pythonVersion = "3.12"
typeCheckingMode = "basic"

[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]
```

- [ ] **Step 2: Create `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
data/
*.db
.venv/
.env
.env.local
```

- [ ] **Step 3: Create `.dockerignore`**

```
.git
.github
.venv
__pycache__
*.pyc
.pytest_cache
.ruff_cache
.coverage
htmlcov
data
docs
tests
*.md
!README.md
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml .gitignore .dockerignore
git commit -m "chore: project scaffolding (pyproject, gitignore, dockerignore)"
```

### Task 1.2: Hello-World FastAPI app

**Files:**
- Create: `app/__init__.py` (empty)
- Create: `app/main.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`
- Create: `tests/test_main.py`

- [ ] **Step 1: Write the failing test**

`tests/test_main.py`:
```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_root_returns_200():
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "yt-summary" in response.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: Create `tests/conftest.py` (empty placeholder for now)**

```python
# Shared pytest fixtures live here. Filled in later phases.
```

- [ ] **Step 4: Create empty `app/__init__.py` and `tests/__init__.py`**

Both are empty files (zero bytes).

- [ ] **Step 5: Implement `app/main.py`**

```python
from fastapi import FastAPI
from fastapi.responses import HTMLResponse


def create_app() -> FastAPI:
    app = FastAPI(title="yt-summary")

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return "<h1>yt-summary</h1>"

    return app


app = create_app()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_main.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app tests
git commit -m "feat: hello-world FastAPI app with smoke test"
```

### Task 1.3: README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README"
```

---

## Phase 2: Database & Models

Goal: schema in place, repos for every table tested in isolation.

### Task 2.1: Config module

**Files:**
- Create: `app/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from app.config import Config


def test_config_default_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.data_dir == tmp_path
    assert cfg.db_path == tmp_path / "app.db"
    assert cfg.thumbnails_dir == tmp_path / "thumbnails"
    assert cfg.audio_dir == tmp_path / "audio"
    assert cfg.cookies_path == tmp_path / "cookies.txt"


def test_config_creates_subdirs(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    cfg.ensure_dirs()
    assert (tmp_path / "thumbnails").is_dir()
    assert (tmp_path / "audio").is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement `app/config.py`**

```python
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def cookies_path(self) -> Path:
        return self.data_dir / "cookies.txt"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(data_dir=Path(os.environ.get("YTS_DATA_DIR", "/data")))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: Config dataclass with env-driven data_dir"
```

### Task 2.2: Database schema and connection

**Files:**
- Create: `app/db.py`
- Create: `tests/test_db.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add fixture to `tests/conftest.py`**

```python
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.config import Config
from app.db import connect, init_schema


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


@pytest_asyncio.fixture
async def db(config: Config) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(config)
    await init_schema(conn)
    yield conn
    await conn.close()
```

- [ ] **Step 2: Write the failing test**

`tests/test_db.py`:
```python
import aiosqlite

from app.db import init_schema


async def test_schema_creates_all_tables(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert {"videos", "jobs", "chat_messages", "settings"}.issubset(tables)


async def test_schema_creates_fts(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='videos_fts'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_init_schema_is_idempotent(db: aiosqlite.Connection):
    await init_schema(db)
    await init_schema(db)
    cursor = await db.execute("SELECT COUNT(*) FROM videos")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0
```

- [ ] **Step 3: Run, verify failure**

Run: `pytest tests/test_db.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 4: Implement `app/db.py`**

```python
import aiosqlite

from app.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    thumbnail_path TEXT,
    duration_seconds INTEGER,
    transcript TEXT,
    transcript_source TEXT,
    summary TEXT,
    summary_model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(id),
    state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
    step TEXT NOT NULL DEFAULT '',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_video_created ON chat_messages(video_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    id UNINDEXED,
    title,
    description,
    content='videos',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;
"""


async def connect(config: Config) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(config.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await conn.commit()
```

- [ ] **Step 5: Run, verify pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add app/db.py tests/test_db.py tests/conftest.py
git commit -m "feat: SQLite schema with FTS5 over videos"
```

### Task 2.3: Models (dataclasses)

**Files:**
- Create: `app/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
from datetime import datetime

from app.models import ChatMessage, Job, JobState, TranscriptSource, Video


def test_video_dataclass():
    v = Video(
        id="abc123",
        url="https://youtu.be/abc123",
        title="Hello",
        description="desc",
        thumbnail_path=None,
        duration_seconds=600,
        transcript=None,
        transcript_source=None,
        summary=None,
        summary_model=None,
        created_at=datetime(2026, 5, 5),
        updated_at=datetime(2026, 5, 5),
    )
    assert v.id == "abc123"
    assert v.summary is None


def test_transcript_source_enum_values():
    assert TranscriptSource.MANUAL_SUBS.value == "manual_subs"
    assert TranscriptSource.AUTO_SUBS.value == "auto_subs"
    assert TranscriptSource.WHISPER.value == "whisper"


def test_job_state_enum_values():
    assert {s.value for s in JobState} == {"pending", "running", "done", "failed"}


def test_chat_message_dataclass():
    msg = ChatMessage(
        id=1,
        video_id="abc",
        role="user",
        content="hi",
        created_at=datetime(2026, 5, 5),
    )
    assert msg.role == "user"


def test_job_dataclass():
    j = Job(
        id=1,
        video_id="abc",
        state=JobState.PENDING,
        step="",
        error_message=None,
        created_at=datetime(2026, 5, 5),
        updated_at=datetime(2026, 5, 5),
    )
    assert j.state is JobState.PENDING
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_models.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `app/models.py`**

```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


class TranscriptSource(StrEnum):
    MANUAL_SUBS = "manual_subs"
    AUTO_SUBS = "auto_subs"
    WHISPER = "whisper"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


ChatRole = Literal["user", "assistant"]


@dataclass
class Video:
    id: str
    url: str
    title: str
    description: str
    thumbnail_path: str | None
    duration_seconds: int | None
    transcript: str | None
    transcript_source: TranscriptSource | None
    summary: str | None
    summary_model: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class Job:
    id: int
    video_id: str
    state: JobState
    step: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class ChatMessage:
    id: int
    video_id: str
    role: ChatRole
    content: str
    created_at: datetime
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: dataclass models for Video/Job/ChatMessage and enums"
```

### Task 2.4: Videos repo

**Files:**
- Create: `app/repos/__init__.py` (empty)
- Create: `app/repos/videos.py`
- Create: `tests/test_repos_videos.py`

- [ ] **Step 1: Write the failing test**

`tests/test_repos_videos.py`:
```python
import aiosqlite

from app.models import TranscriptSource, Video
from app.repos import videos as videos_repo


async def _insert_sample(db: aiosqlite.Connection, vid: str = "abc123") -> None:
    await videos_repo.upsert_metadata(
        db,
        video_id=vid,
        url=f"https://youtu.be/{vid}",
        title="Hello",
        description="A nice video",
        thumbnail_path=None,
        duration_seconds=600,
    )


async def test_upsert_metadata_creates_row(db: aiosqlite.Connection):
    await _insert_sample(db)
    v = await videos_repo.get(db, "abc123")
    assert v is not None
    assert v.title == "Hello"
    assert v.summary is None


async def test_upsert_metadata_idempotent_keeps_transcript(db: aiosqlite.Connection):
    await _insert_sample(db)
    await videos_repo.set_transcript(db, "abc123", "the words", TranscriptSource.AUTO_SUBS)
    await _insert_sample(db)  # second upsert, simulating re-submit
    v = await videos_repo.get(db, "abc123")
    assert v is not None
    assert v.transcript == "the words"
    assert v.transcript_source is TranscriptSource.AUTO_SUBS


async def test_set_summary(db: aiosqlite.Connection):
    await _insert_sample(db)
    await videos_repo.set_summary(db, "abc123", "TL;DR", "openai/gpt-4o")
    v = await videos_repo.get(db, "abc123")
    assert v is not None
    assert v.summary == "TL;DR"
    assert v.summary_model == "openai/gpt-4o"


async def test_list_recent_orders_by_created_desc(db: aiosqlite.Connection):
    await _insert_sample(db, "a")
    await _insert_sample(db, "b")
    await _insert_sample(db, "c")
    rows = await videos_repo.list_recent(db, limit=10)
    ids = [v.id for v in rows]
    assert ids == ["c", "b", "a"]


async def test_search_uses_fts(db: aiosqlite.Connection):
    await videos_repo.upsert_metadata(
        db,
        video_id="x1",
        url="u",
        title="Python tutorial",
        description="learn fastapi",
        thumbnail_path=None,
        duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db,
        video_id="x2",
        url="u",
        title="Cooking pasta",
        description="italian food",
        thumbnail_path=None,
        duration_seconds=None,
    )
    results = await videos_repo.search(db, "fastapi")
    assert [v.id for v in results] == ["x1"]


async def test_get_returns_none_for_missing(db: aiosqlite.Connection):
    assert await videos_repo.get(db, "nope") is None
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_repos_videos.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Create `app/repos/__init__.py` (empty file)**

- [ ] **Step 4: Implement `app/repos/videos.py`**

```python
from datetime import datetime

import aiosqlite

from app.models import TranscriptSource, Video


def _row_to_video(row: aiosqlite.Row) -> Video:
    src = row["transcript_source"]
    return Video(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        thumbnail_path=row["thumbnail_path"],
        duration_seconds=row["duration_seconds"],
        transcript=row["transcript"],
        transcript_source=TranscriptSource(src) if src else None,
        summary=row["summary"],
        summary_model=row["summary_model"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def upsert_metadata(
    db: aiosqlite.Connection,
    *,
    video_id: str,
    url: str,
    title: str,
    description: str,
    thumbnail_path: str | None,
    duration_seconds: int | None,
) -> None:
    await db.execute(
        """
        INSERT INTO videos (id, url, title, description, thumbnail_path, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            description=excluded.description,
            thumbnail_path=COALESCE(excluded.thumbnail_path, videos.thumbnail_path),
            duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds),
            updated_at=datetime('now')
        """,
        (video_id, url, title, description, thumbnail_path, duration_seconds),
    )
    await db.commit()


async def set_transcript(
    db: aiosqlite.Connection,
    video_id: str,
    transcript: str,
    source: TranscriptSource,
) -> None:
    await db.execute(
        "UPDATE videos SET transcript=?, transcript_source=?, updated_at=datetime('now') WHERE id=?",
        (transcript, source.value, video_id),
    )
    await db.commit()


async def set_summary(
    db: aiosqlite.Connection,
    video_id: str,
    summary: str,
    model: str,
) -> None:
    await db.execute(
        "UPDATE videos SET summary=?, summary_model=?, updated_at=datetime('now') WHERE id=?",
        (summary, model, video_id),
    )
    await db.commit()


async def get(db: aiosqlite.Connection, video_id: str) -> Video | None:
    cursor = await db.execute("SELECT * FROM videos WHERE id=?", (video_id,))
    row = await cursor.fetchone()
    return _row_to_video(row) if row else None


async def list_recent(db: aiosqlite.Connection, limit: int = 50) -> list[Video]:
    cursor = await db.execute(
        "SELECT * FROM videos ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]


async def search(db: aiosqlite.Connection, query: str, limit: int = 50) -> list[Video]:
    cursor = await db.execute(
        """
        SELECT v.* FROM videos v
        JOIN videos_fts f ON v.rowid = f.rowid
        WHERE videos_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]
```

- [ ] **Step 5: Run, verify pass**

Run: `pytest tests/test_repos_videos.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add app/repos tests/test_repos_videos.py
git commit -m "feat: videos repo with upsert/get/list/search (FTS5)"
```

### Task 2.5: Jobs repo (queue operations)

**Files:**
- Create: `app/repos/jobs.py`
- Create: `tests/test_repos_jobs.py`

- [ ] **Step 1: Write the failing test**

`tests/test_repos_jobs.py`:
```python
import aiosqlite

from app.models import JobState
from app.repos import jobs as jobs_repo
from app.repos import videos as videos_repo


async def _video(db: aiosqlite.Connection, vid: str = "v1") -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


async def test_enqueue_creates_pending_job(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.PENDING
    assert job.video_id == "v1"


async def test_claim_next_returns_oldest_pending(db: aiosqlite.Connection):
    await _video(db, "a")
    await _video(db, "b")
    j1 = await jobs_repo.enqueue(db, "a")
    j2 = await jobs_repo.enqueue(db, "b")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None
    assert claimed.id == j1
    assert claimed.state is JobState.RUNNING
    # Second claim picks the next one
    claimed2 = await jobs_repo.claim_next(db)
    assert claimed2 is not None
    assert claimed2.id == j2


async def test_claim_next_returns_none_when_no_pending(db: aiosqlite.Connection):
    assert await jobs_repo.claim_next(db) is None


async def test_set_step_updates_step(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.set_step(db, job_id, "downloading audio")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.step == "downloading audio"


async def test_complete_marks_done(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.complete(db, job_id)
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.DONE


async def test_fail_marks_failed_with_message(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    await jobs_repo.fail(db, job_id, "oops")
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.FAILED
    assert job.error_message == "oops"


async def test_reset_running_to_pending(db: aiosqlite.Connection):
    await _video(db)
    job_id = await jobs_repo.enqueue(db, "v1")
    claimed = await jobs_repo.claim_next(db)
    assert claimed is not None
    await jobs_repo.reset_orphaned_running(db)
    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.PENDING


async def test_latest_for_video(db: aiosqlite.Connection):
    await _video(db)
    j1 = await jobs_repo.enqueue(db, "v1")
    j2 = await jobs_repo.enqueue(db, "v1")
    latest = await jobs_repo.latest_for_video(db, "v1")
    assert latest is not None
    assert latest.id == j2
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_repos_jobs.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `app/repos/jobs.py`**

```python
from datetime import datetime

import aiosqlite

from app.models import Job, JobState


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        id=row["id"],
        video_id=row["video_id"],
        state=JobState(row["state"]),
        step=row["step"],
        error_message=row["error_message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def enqueue(db: aiosqlite.Connection, video_id: str) -> int:
    cursor = await db.execute(
        "INSERT INTO jobs (video_id, state) VALUES (?, 'pending')",
        (video_id,),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def claim_next(db: aiosqlite.Connection) -> Job | None:
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE state = 'pending'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    if row is None:
        await db.commit()
        return None
    job_id = row["id"]
    await db.execute(
        "UPDATE jobs SET state='running', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    await db.commit()
    return await get(db, job_id)


async def get(db: aiosqlite.Connection, job_id: int) -> Job | None:
    cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def latest_for_video(db: aiosqlite.Connection, video_id: str) -> Job | None:
    cursor = await db.execute(
        "SELECT * FROM jobs WHERE video_id=? ORDER BY id DESC LIMIT 1",
        (video_id,),
    )
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def set_step(db: aiosqlite.Connection, job_id: int, step: str) -> None:
    await db.execute(
        "UPDATE jobs SET step=?, updated_at=datetime('now') WHERE id=?",
        (step, job_id),
    )
    await db.commit()


async def complete(db: aiosqlite.Connection, job_id: int) -> None:
    await db.execute(
        "UPDATE jobs SET state='done', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    await db.commit()


async def fail(db: aiosqlite.Connection, job_id: int, message: str) -> None:
    await db.execute(
        "UPDATE jobs SET state='failed', error_message=?, updated_at=datetime('now') WHERE id=?",
        (message, job_id),
    )
    await db.commit()


async def reset_orphaned_running(db: aiosqlite.Connection) -> None:
    """Called at startup. Jobs left running across a restart go back to pending."""
    await db.execute(
        "UPDATE jobs SET state='pending', updated_at=datetime('now') WHERE state='running'"
    )
    await db.commit()
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_repos_jobs.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/repos/jobs.py tests/test_repos_jobs.py
git commit -m "feat: jobs repo (enqueue, claim_next, complete, fail, reset)"
```

### Task 2.6: Chat repo

**Files:**
- Create: `app/repos/chat.py`
- Create: `tests/test_repos_chat.py`

- [ ] **Step 1: Write the failing test**

```python
import aiosqlite

from app.repos import chat as chat_repo
from app.repos import videos as videos_repo


async def _video(db: aiosqlite.Connection) -> None:
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


async def test_append_and_history(db: aiosqlite.Connection):
    await _video(db)
    await chat_repo.append(db, "v1", "user", "what's it about?")
    await chat_repo.append(db, "v1", "assistant", "summary text")
    msgs = await chat_repo.history(db, "v1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "summary text"


async def test_history_empty_for_unknown_video(db: aiosqlite.Connection):
    assert await chat_repo.history(db, "nope") == []
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_repos_chat.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `app/repos/chat.py`**

```python
from datetime import datetime

import aiosqlite

from app.models import ChatMessage, ChatRole


def _row_to_msg(row: aiosqlite.Row) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        video_id=row["video_id"],
        role=row["role"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def append(
    db: aiosqlite.Connection, video_id: str, role: ChatRole, content: str
) -> ChatMessage:
    cursor = await db.execute(
        "INSERT INTO chat_messages (video_id, role, content) VALUES (?, ?, ?)",
        (video_id, role, content),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    fetched = await db.execute(
        "SELECT * FROM chat_messages WHERE id=?", (cursor.lastrowid,)
    )
    row = await fetched.fetchone()
    assert row is not None
    return _row_to_msg(row)


async def history(db: aiosqlite.Connection, video_id: str) -> list[ChatMessage]:
    cursor = await db.execute(
        "SELECT * FROM chat_messages WHERE video_id=? ORDER BY created_at ASC, id ASC",
        (video_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_msg(r) for r in rows]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_repos_chat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repos/chat.py tests/test_repos_chat.py
git commit -m "feat: chat repo (append, history)"
```

### Task 2.7: Settings repo

**Files:**
- Create: `app/repos/settings.py`
- Create: `tests/test_repos_settings.py`

- [ ] **Step 1: Write the failing test**

```python
import aiosqlite

from app.repos import settings as settings_repo


async def test_get_returns_none_when_unset(db: aiosqlite.Connection):
    assert await settings_repo.get(db, "llm_model") is None


async def test_set_then_get(db: aiosqlite.Connection):
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    assert await settings_repo.get(db, "llm_model") == "openai/gpt-4o"


async def test_set_overwrites(db: aiosqlite.Connection):
    await settings_repo.set(db, "llm_model", "a")
    await settings_repo.set(db, "llm_model", "b")
    assert await settings_repo.get(db, "llm_model") == "b"


async def test_get_all_returns_dict(db: aiosqlite.Connection):
    await settings_repo.set(db, "k1", "v1")
    await settings_repo.set(db, "k2", "v2")
    assert await settings_repo.get_all(db) == {"k1": "v1", "k2": "v2"}


async def test_delete(db: aiosqlite.Connection):
    await settings_repo.set(db, "k", "v")
    await settings_repo.delete(db, "k")
    assert await settings_repo.get(db, "k") is None
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement `app/repos/settings.py`**

```python
import aiosqlite


async def get(db: aiosqlite.Connection, key: str) -> str | None:
    cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cursor.fetchone()
    return row[0] if row else None


async def set(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )
    await db.commit()


async def get_all(db: aiosqlite.Connection) -> dict[str, str]:
    cursor = await db.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def delete(db: aiosqlite.Connection, key: str) -> None:
    await db.execute("DELETE FROM settings WHERE key=?", (key,))
    await db.commit()
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_repos_settings.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/repos/settings.py tests/test_repos_settings.py
git commit -m "feat: settings key-value repo"
```

---

## Phase 3: yt-dlp metadata service & video submission flow

Goal: paste a URL, get a card immediately with title/thumbnail/description.

### Task 3.1: yt-dlp metadata wrapper

**Files:**
- Create: `app/services/__init__.py` (empty)
- Create: `app/services/youtube.py`
- Create: `tests/test_services_youtube.py`
- Create: `tests/fixtures/yt_dlp_metadata.json`

- [ ] **Step 1: Capture a real yt-dlp metadata response (manual; one-time)**

Run this command locally and save the truncated result to `tests/fixtures/yt_dlp_metadata.json`. (If yt-dlp isn't installed yet, install it via the project's `pip install -e .` first.)

Manual capture for fixture only — the test itself uses a hand-trimmed JSON. Save this content into `tests/fixtures/yt_dlp_metadata.json`:

```json
{
  "id": "dQw4w9WgXcQ",
  "title": "Sample Title",
  "description": "Sample description text.",
  "duration": 212,
  "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
  "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

- [ ] **Step 2: Write the failing test**

`tests/test_services_youtube.py`:
```python
import json
from pathlib import Path
from unittest.mock import patch

from app.services.youtube import VideoMetadata, fetch_metadata, parse_video_id

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_video_id_short_url():
    assert parse_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_video_id_watch_url():
    assert parse_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s") == "dQw4w9WgXcQ"


def test_parse_video_id_shorts_url():
    assert parse_video_id("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_video_id_invalid_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_video_id("https://example.com/foo")


async def test_fetch_metadata_returns_dataclass(tmp_path):
    fixture = json.loads((FIXTURES / "yt_dlp_metadata.json").read_text())
    with patch("app.services.youtube._extract_info", return_value=fixture):
        meta = await fetch_metadata("https://youtu.be/dQw4w9WgXcQ", cookies_path=None)
    assert isinstance(meta, VideoMetadata)
    assert meta.id == "dQw4w9WgXcQ"
    assert meta.title == "Sample Title"
    assert meta.duration_seconds == 212
    assert meta.thumbnail_url.endswith(".jpg")
```

- [ ] **Step 3: Run, verify failure**

Run: `pytest tests/test_services_youtube.py -v`
Expected: FAIL.

- [ ] **Step 4: Implement `app/services/youtube.py`**

```python
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

_VIDEO_ID_RE = re.compile(
    r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})"
)


def parse_video_id(url: str) -> str:
    match = _VIDEO_ID_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract video id from {url!r}")
    return match.group(1)


@dataclass(frozen=True)
class VideoMetadata:
    id: str
    url: str
    title: str
    description: str
    duration_seconds: int | None
    thumbnail_url: str | None


def _extract_info(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {"skip_download": True, "quiet": True, "no_warnings": True}
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


async def fetch_metadata(url: str, cookies_path: Path | None) -> VideoMetadata:
    info = await asyncio.to_thread(_extract_info, url, cookies_path)
    return VideoMetadata(
        id=info["id"],
        url=info.get("webpage_url", url),
        title=info.get("title", ""),
        description=info.get("description") or "",
        duration_seconds=info.get("duration"),
        thumbnail_url=info.get("thumbnail"),
    )
```

- [ ] **Step 5: Run, verify pass**

Run: `pytest tests/test_services_youtube.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add app/services/__init__.py app/services/youtube.py tests/test_services_youtube.py tests/fixtures/yt_dlp_metadata.json
git commit -m "feat: yt-dlp metadata service with URL parsing"
```

### Task 3.2: Thumbnail download helper

**Files:**
- Modify: `app/services/youtube.py` (add function)
- Modify: `tests/test_services_youtube.py` (add tests)

- [ ] **Step 1: Add the failing test to `tests/test_services_youtube.py`**

Append:
```python
import respx
from httpx import Response


async def test_download_thumbnail_writes_file(tmp_path):
    from app.services.youtube import download_thumbnail
    target = tmp_path / "thumb.jpg"
    fake_jpeg = b"\xff\xd8\xff\xe0fakejpeg"
    with respx.mock:
        respx.get("https://img.example/thumb.jpg").mock(
            return_value=Response(200, content=fake_jpeg)
        )
        await download_thumbnail("https://img.example/thumb.jpg", target)
    assert target.read_bytes() == fake_jpeg


async def test_download_thumbnail_handles_missing_url(tmp_path):
    from app.services.youtube import download_thumbnail
    target = tmp_path / "thumb.jpg"
    await download_thumbnail(None, target)
    assert not target.exists()
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Add `download_thumbnail` to `app/services/youtube.py`**

Append at the bottom:
```python
import httpx


async def download_thumbnail(url: str | None, target: Path) -> None:
    if not url:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        target.write_bytes(resp.content)
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add app/services/youtube.py tests/test_services_youtube.py
git commit -m "feat: download_thumbnail helper"
```

### Task 3.3: App lifespan, DB connection, dependency wiring

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Write the failing test**

Replace `tests/test_main.py` with:
```python
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app


def test_root_returns_200(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        response = client.get("/")
    assert response.status_code == 200


def test_app_creates_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        client.get("/")
    assert (tmp_path / "thumbnails").is_dir()
    assert (tmp_path / "audio").is_dir()
    assert (tmp_path / "app.db").is_file()
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_main.py -v`
Expected: FAIL — second test will fail because lifespan doesn't init DB yet.

- [ ] **Step 3: Implement lifespan in `app/main.py`**

Replace contents:
```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.db import connect, init_schema
from app.repos import jobs as jobs_repo


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)
    app.state.config = config
    app.state.db = db
    try:
        yield
    finally:
        await db.close()


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def get_config(request: Request) -> Config:
    return request.app.state.config


def create_app() -> FastAPI:
    app = FastAPI(title="yt-summary", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return "<h1>yt-summary</h1>"

    return app


app = create_app()
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_main.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: lifespan with DB init and orphan-job reset"
```

### Task 3.4: Templates skeleton + home route

**Files:**
- Create: `app/templates/base.html`
- Create: `app/templates/home.html`
- Create: `app/templates/video_card.html`
- Create: `app/routes/__init__.py` (empty)
- Create: `app/routes/home.py`
- Modify: `app/main.py` (mount router and templates)
- Create: `tests/test_routes_home.py`

- [ ] **Step 1: Write the failing test**

`tests/test_routes_home.py`:
```python
from fastapi.testclient import TestClient

from app.main import create_app
from app.repos import videos as videos_repo


def test_home_lists_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        # Insert a video by reaching into the connection (test-only shortcut).
        import asyncio

        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db,
                video_id="v1",
                url="https://youtu.be/v1",
                title="Test Video",
                description="d",
                thumbnail_path=None,
                duration_seconds=120,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Test Video" in resp.text


def test_home_search(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="a", url="u",
                title="Python tutorial", description="fastapi",
                thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="b", url="u",
                title="Cooking", description="pasta",
                thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/?q=fastapi")
    assert "Python tutorial" in resp.text
    assert "Cooking" not in resp.text
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_routes_home.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `app/templates/base.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}yt-summary{% endblock %}</title>
  <link rel="stylesheet" href="{{ url_for('static', path='app.css') }}">
  <script src="{{ url_for('static', path='htmx.min.js') }}" defer></script>
  <script src="{{ url_for('static', path='alpine.min.js') }}" defer></script>
</head>
<body>
  <header>
    <a href="/" class="brand">yt-summary</a>
    <a href="/settings" class="gear" aria-label="Settings">⚙️</a>
  </header>
  <main>
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 4: Create `app/templates/home.html`**

```html
{% extends "base.html" %}
{% block content %}
<form method="post" action="/videos" class="submit-form">
  <input type="url" name="url" placeholder="Paste YouTube URL" required>
  <button type="submit">Summarize</button>
</form>

<form method="get" action="/" class="search-form">
  <input type="search" name="q" value="{{ q or '' }}" placeholder="Search past videos">
  <button type="submit">Search</button>
</form>

<section id="video-list">
  {% for video in videos %}
    {% include "video_card.html" %}
  {% else %}
    <p class="empty">No videos yet.</p>
  {% endfor %}
</section>
{% endblock %}
```

- [ ] **Step 5: Create `app/templates/video_card.html`**

```html
<article class="video-card" id="video-{{ video.id }}">
  <a href="/v/{{ video.id }}">
    {% if video.thumbnail_path %}
      <img src="/thumbnails/{{ video.id }}.jpg" alt="">
    {% endif %}
    <h3>{{ video.title }}</h3>
  </a>
  {% if video.summary %}
    <p class="status status-done">✓ summary ready</p>
  {% else %}
    <p class="status" hx-get="/v/{{ video.id }}/status" hx-trigger="load, every 2s" hx-swap="outerHTML">
      …
    </p>
  {% endif %}
</article>
```

- [ ] **Step 6: Create `app/routes/__init__.py` (empty)**

- [ ] **Step 7: Implement `app/routes/home.py`**

```python
import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.main import get_db
from app.repos import videos as videos_repo

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    if q:
        videos = await videos_repo.search(db, q)
    else:
        videos = await videos_repo.list_recent(db)
    return templates.TemplateResponse(
        request, "home.html", {"videos": videos, "q": q}
    )
```

- [ ] **Step 8: Update `app/main.py` to mount router, static files, and serve thumbnails**

Replace the `create_app` function:
```python
def create_app() -> FastAPI:
    from app.routes.home import router as home_router

    app = FastAPI(title="yt-summary", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/thumbnails/{video_id}.jpg")
    async def thumbnail(video_id: str, request: Request):
        from fastapi.responses import FileResponse
        cfg: Config = request.app.state.config
        path = cfg.thumbnails_dir / f"{video_id}.jpg"
        if not path.exists():
            from fastapi import HTTPException
            raise HTTPException(404)
        return FileResponse(path)

    app.include_router(home_router)
    return app


app = create_app()
```

Also delete the old `@app.get("/")` route inside `create_app`.

- [ ] **Step 9: Create empty placeholder static files**

```bash
mkdir -p app/static
touch app/static/htmx.min.js app/static/alpine.min.js app/static/app.css
```

- [ ] **Step 10: Run, verify pass**

Run: `pytest tests/test_routes_home.py tests/test_main.py -v`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add app/templates app/routes app/main.py app/static tests/test_routes_home.py
git commit -m "feat: home route with templates, search, and HTMX scaffolding"
```

### Task 3.5: POST /videos (submit URL)

**Files:**
- Create: `app/routes/videos.py`
- Modify: `app/main.py`
- Create: `tests/test_routes_videos.py`

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.youtube import VideoMetadata


def test_post_videos_creates_card_and_enqueues_job(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake_meta = VideoMetadata(
        id="abc12345678",
        url="https://youtu.be/abc12345678",
        title="A test video",
        description="cool",
        duration_seconds=300,
        thumbnail_url="https://example.com/t.jpg",
    )
    app = create_app()
    with (
        patch("app.routes.videos.fetch_metadata", AsyncMock(return_value=fake_meta)),
        patch("app.routes.videos.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post("/videos", data={"url": "https://youtu.be/abc12345678"})
    assert resp.status_code == 200
    assert "A test video" in resp.text
    assert "abc12345678" in resp.text


def test_post_videos_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/videos", data={"url": "not a url"})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_routes_videos.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `app/routes/videos.py`**

```python
import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_db
from app.repos import jobs as jobs_repo
from app.repos import videos as videos_repo
from app.services.youtube import download_thumbnail, fetch_metadata, parse_video_id

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.post("/videos", response_class=HTMLResponse)
async def submit_video(
    request: Request,
    url: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    try:
        parse_video_id(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    cookies = config.cookies_path if config.cookies_path.exists() else None
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
    )
    await jobs_repo.enqueue(db, meta.id)
    video = await videos_repo.get(db, meta.id)
    return templates.TemplateResponse(
        request, "video_card.html", {"video": video}
    )
```

- [ ] **Step 4: Wire router in `app/main.py`**

In `create_app`, after `app.include_router(home_router)` add:
```python
from app.routes.videos import router as videos_router
app.include_router(videos_router)
```

- [ ] **Step 5: Run, verify pass**

Run: `pytest tests/test_routes_videos.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routes/videos.py app/main.py tests/test_routes_videos.py
git commit -m "feat: POST /videos creates card and enqueues job"
```

### Task 3.6: GET /v/{id}/status fragment

**Files:**
- Create: `app/templates/video_status.html`
- Modify: `app/routes/videos.py` (add route)
- Modify: `tests/test_routes_videos.py` (add tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_routes_videos.py`:
```python
def test_status_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import jobs as jobs_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await jobs_repo.enqueue(app.state.db, "v1")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1/status")
    assert resp.status_code == 200
    assert "pending" in resp.text.lower() or "queued" in resp.text.lower()


def test_status_done_summary_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(app.state.db, "v1", "the summary", "openai/gpt-4o")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1/status")
    assert "summary ready" in resp.text.lower()
    # No HTMX poll trigger expected for done state
    assert "every 2s" not in resp.text
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Create `app/templates/video_status.html`**

```html
{% if video.summary %}
  <p class="status status-done">✓ summary ready</p>
{% elif job and job.state.value == 'failed' %}
  <p class="status status-failed">⚠ {{ job.error_message or 'failed' }}</p>
{% elif job and job.state.value == 'running' %}
  <p class="status status-running" hx-get="/v/{{ video.id }}/status" hx-trigger="every 2s" hx-swap="outerHTML">
    {{ job.step or 'running' }}…
  </p>
{% else %}
  <p class="status status-pending" hx-get="/v/{{ video.id }}/status" hx-trigger="every 2s" hx-swap="outerHTML">
    queued…
  </p>
{% endif %}
```

- [ ] **Step 4: Add route in `app/routes/videos.py`**

Append:
```python
@router.get("/v/{video_id}/status", response_class=HTMLResponse)
async def video_status(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    job = await jobs_repo.latest_for_video(db, video_id)
    return templates.TemplateResponse(
        request, "video_status.html", {"video": video, "job": job}
    )
```

- [ ] **Step 5: Run, verify pass**

- [ ] **Step 6: Commit**

```bash
git add app/templates/video_status.html app/routes/videos.py tests/test_routes_videos.py
git commit -m "feat: GET /v/{id}/status HTMX fragment"
```

### Phase 3 verification

Run a manual smoke test (informational, not a test step):
```bash
uvicorn app.main:app --reload
```
Open http://localhost:8000, paste a YouTube URL. You should see a video card appear with title and thumbnail. Status will show "queued..." indefinitely (no worker yet).

---

## Phase 4: Worker

Goal: jobs actually run. For now they only set status; no transcript/summary yet.

### Task 4.1: Worker scaffold (no pipeline yet)

**Files:**
- Create: `app/worker.py`
- Modify: `app/main.py` (start worker on lifespan)
- Create: `tests/test_worker.py`

- [ ] **Step 1: Write the failing test**

`tests/test_worker.py`:
```python
import asyncio
from unittest.mock import AsyncMock

import aiosqlite

from app.models import JobState
from app.repos import jobs as jobs_repo
from app.repos import videos as videos_repo
from app.worker import Worker


async def test_worker_processes_pending_job(db: aiosqlite.Connection, tmp_path, monkeypatch):
    from app.config import Config
    config = Config(data_dir=tmp_path)

    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_id = await jobs_repo.enqueue(db, "v1")

    process_video = AsyncMock(return_value=None)
    worker = Worker(db=db, config=config, process_video=process_video)
    task = asyncio.create_task(worker.run())
    # Let it pick up and process
    for _ in range(20):
        await asyncio.sleep(0.05)
        job = await jobs_repo.get(db, job_id)
        if job and job.state is JobState.DONE:
            break
    worker.stop()
    await task

    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.DONE
    process_video.assert_awaited_once()


async def test_worker_marks_failed_on_exception(db: aiosqlite.Connection, tmp_path):
    from app.config import Config
    config = Config(data_dir=tmp_path)

    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    job_id = await jobs_repo.enqueue(db, "v1")

    async def boom(db, config, video_id, set_step):
        raise RuntimeError("kaboom")

    worker = Worker(db=db, config=config, process_video=boom)
    task = asyncio.create_task(worker.run())
    for _ in range(20):
        await asyncio.sleep(0.05)
        job = await jobs_repo.get(db, job_id)
        if job and job.state is JobState.FAILED:
            break
    worker.stop()
    await task

    job = await jobs_repo.get(db, job_id)
    assert job is not None
    assert job.state is JobState.FAILED
    assert job.error_message is not None
    assert "kaboom" in job.error_message
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_worker.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `app/worker.py`**

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiosqlite

from app.config import Config
from app.repos import jobs as jobs_repo

log = logging.getLogger(__name__)

ProcessVideo = Callable[
    [aiosqlite.Connection, Config, str, Callable[[str], Awaitable[None]]],
    Awaitable[None],
]


class Worker:
    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        process_video: ProcessVideo,
        poll_interval: float = 1.0,
    ):
        self._db = db
        self._config = config
        self._process_video = process_video
        self._poll_interval = poll_interval
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            job = await jobs_repo.claim_next(self._db)
            if job is None:
                try:
                    await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                async def set_step(step: str) -> None:
                    await jobs_repo.set_step(self._db, job.id, step)

                await self._process_video(self._db, self._config, job.video_id, set_step)
                await jobs_repo.complete(self._db, job.id)
            except Exception as e:
                log.exception("job %s failed", job.id)
                await jobs_repo.fail(self._db, job.id, str(e))
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/test_worker.py
git commit -m "feat: Worker class with FIFO claim, success/fail handling"
```

### Task 4.2: Wire worker into app lifespan

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Update `lifespan` in `app/main.py`**

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.pipeline import process_video  # imported here to avoid circular
    from app.worker import Worker

    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    await jobs_repo.reset_orphaned_running(db)

    worker = Worker(db=db, config=config, process_video=process_video)
    worker_task = asyncio.create_task(worker.run())

    app.state.config = config
    app.state.db = db
    app.state.worker = worker
    try:
        yield
    finally:
        worker.stop()
        await worker_task
        await db.close()
```

Add `import asyncio` at the top of the file.

- [ ] **Step 2: Create stub `app/pipeline.py` so import works**

```python
from collections.abc import Awaitable, Callable

import aiosqlite

from app.config import Config


async def process_video(
    db: aiosqlite.Connection,
    config: Config,
    video_id: str,
    set_step: Callable[[str], Awaitable[None]],
) -> None:
    """Pipeline stub. Real implementation lands in Phase 5+."""
    await set_step("done (stub)")
```

- [ ] **Step 3: Verify the existing test suite still passes**

Run: `pytest -v`
Expected: PASS for everything.

- [ ] **Step 4: Commit**

```bash
git add app/main.py app/pipeline.py
git commit -m "feat: start worker on app lifespan with pipeline stub"
```

---

## Phase 5: Transcript pipeline

Goal: real subtitles via yt-dlp, with faster-whisper fallback.

### Task 5.1: yt-dlp subtitle/auto-caption fetch

**Files:**
- Modify: `app/services/youtube.py` (add functions)
- Modify: `tests/test_services_youtube.py` (add tests)
- Create: `tests/fixtures/sample.vtt`

- [ ] **Step 1: Create the VTT fixture**

`tests/fixtures/sample.vtt`:
```
WEBVTT

00:00:00.000 --> 00:00:02.000
Hello and welcome.

00:00:02.000 --> 00:00:05.000
Today we discuss FastAPI.
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_services_youtube.py`:
```python
def test_vtt_to_plain_text():
    from app.services.youtube import vtt_to_plain_text
    vtt = (FIXTURES / "sample.vtt").read_text()
    text = vtt_to_plain_text(vtt)
    assert "Hello and welcome." in text
    assert "FastAPI" in text
    assert "WEBVTT" not in text
    assert "-->" not in text


async def test_fetch_subtitles_prefers_manual(tmp_path):
    from app.services.youtube import fetch_subtitles
    fake_info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "https://example.com/manual.vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example.com/auto.vtt"}]},
    }
    with patch("app.services.youtube._extract_info", return_value=fake_info), \
         patch("app.services.youtube._download_text", AsyncMock(return_value=(FIXTURES / "sample.vtt").read_text())):
        from unittest.mock import AsyncMock
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is not None
    text, source = result
    assert "FastAPI" in text
    assert source == "manual_subs"


async def test_fetch_subtitles_falls_back_to_auto(tmp_path):
    from app.services.youtube import fetch_subtitles
    from unittest.mock import AsyncMock
    fake_info = {
        "subtitles": {},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example.com/auto.vtt"}]},
    }
    with patch("app.services.youtube._extract_info", return_value=fake_info), \
         patch("app.services.youtube._download_text", AsyncMock(return_value=(FIXTURES / "sample.vtt").read_text())):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is not None
    _, source = result
    assert source == "auto_subs"


async def test_fetch_subtitles_returns_none_when_unavailable(tmp_path):
    from app.services.youtube import fetch_subtitles
    fake_info = {"subtitles": {}, "automatic_captions": {}}
    with patch("app.services.youtube._extract_info", return_value=fake_info):
        result = await fetch_subtitles("https://youtu.be/x", cookies_path=None)
    assert result is None
```

- [ ] **Step 3: Run, verify failure**

- [ ] **Step 4: Implement subtitle fetching in `app/services/youtube.py`**

Append:
```python
import re as _re
from typing import Literal

import httpx as _httpx

SubtitleSource = Literal["manual_subs", "auto_subs"]


def _extract_info_with_subs(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "de"],
        "subtitlesformat": "vtt",
    }
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


async def _download_text(url: str) -> str:
    async with _httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


_VTT_TIMESTAMP = _re.compile(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}.*")
_VTT_TAG = _re.compile(r"<[^>]+>")


def vtt_to_plain_text(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "WEBVTT":
            continue
        if line.startswith("NOTE") or line.startswith("STYLE") or line.startswith("REGION"):
            continue
        if _VTT_TIMESTAMP.match(line):
            continue
        if line.isdigit():
            continue
        line = _VTT_TAG.sub("", line)
        if line:
            lines.append(line)
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if line not in seen:
            deduped.append(line)
            seen.add(line)
    return "\n".join(deduped)


def _pick_subtitle_url(info: dict[str, Any], key: str) -> str | None:
    subs = info.get(key) or {}
    for lang in ("en", "de"):
        for entry in subs.get(lang) or []:
            if entry.get("ext") == "vtt" and entry.get("url"):
                return entry["url"]
    for entries in subs.values():
        for entry in entries:
            if entry.get("ext") == "vtt" and entry.get("url"):
                return entry["url"]
    return None


async def fetch_subtitles(
    url: str, cookies_path: Path | None
) -> tuple[str, SubtitleSource] | None:
    info = await asyncio.to_thread(_extract_info_with_subs, url, cookies_path)
    manual_url = _pick_subtitle_url(info, "subtitles")
    if manual_url:
        text = await _download_text(manual_url)
        return vtt_to_plain_text(text), "manual_subs"
    auto_url = _pick_subtitle_url(info, "automatic_captions")
    if auto_url:
        text = await _download_text(auto_url)
        return vtt_to_plain_text(text), "auto_subs"
    return None
```

- [ ] **Step 5: Run, verify pass**

- [ ] **Step 6: Commit**

```bash
git add app/services/youtube.py tests/test_services_youtube.py tests/fixtures/sample.vtt
git commit -m "feat: subtitle fetching (manual > auto) with VTT to plain-text"
```

### Task 5.2: Audio download

**Files:**
- Modify: `app/services/youtube.py` (add function)
- Modify: `tests/test_services_youtube.py` (add tests)

- [ ] **Step 1: Add the failing test**

```python
async def test_download_audio_calls_yt_dlp_with_correct_opts(tmp_path):
    from app.services.youtube import download_audio
    captured: dict = {}

    def fake_download(opts, url):
        captured["opts"] = opts
        captured["url"] = url
        # Simulate file creation
        (tmp_path / "vid.m4a").write_bytes(b"fakeaudio")

    with patch("app.services.youtube._run_yt_dlp_download", side_effect=fake_download):
        path = await download_audio("https://youtu.be/x", "vid", tmp_path, cookies_path=None)
    assert path == tmp_path / "vid.m4a"
    assert captured["opts"]["format"].startswith("bestaudio")
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement `download_audio`**

Append to `app/services/youtube.py`:
```python
def _run_yt_dlp_download(opts: dict[str, Any], url: str) -> None:
    with YoutubeDL(opts) as ydl:
        ydl.download([url])


async def download_audio(
    url: str, video_id: str, audio_dir: Path, cookies_path: Path | None
) -> Path:
    audio_dir.mkdir(parents=True, exist_ok=True)
    template = str(audio_dir / f"{video_id}.%(ext)s")
    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
    }
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    await asyncio.to_thread(_run_yt_dlp_download, opts, url)
    for path in audio_dir.iterdir():
        if path.stem == video_id:
            return path
    raise RuntimeError(f"Audio download produced no file for {video_id}")
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add app/services/youtube.py tests/test_services_youtube.py
git commit -m "feat: audio download via yt-dlp"
```

### Task 5.3: Whisper transcription service

**Files:**
- Create: `app/services/whisper.py`
- Create: `tests/test_services_whisper.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_transcribe_returns_concatenated_segments(tmp_path):
    from app.services.whisper import transcribe
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")

    seg1 = MagicMock()
    seg1.text = "Hello and"
    seg2 = MagicMock()
    seg2.text = " welcome."

    fake_model = MagicMock()
    fake_model.transcribe.return_value = (iter([seg1, seg2]), MagicMock())

    with patch("app.services.whisper._load_model", return_value=fake_model):
        result = transcribe(fake_audio, model_name="small")
    assert result.strip() == "Hello and welcome."


def test_load_model_caches_per_name():
    from app.services import whisper as w
    w._MODEL_CACHE.clear()
    with patch("app.services.whisper.WhisperModel") as Model:
        Model.return_value = MagicMock()
        w._load_model("small")
        w._load_model("small")
        assert Model.call_count == 1
        w._load_model("base")
        assert Model.call_count == 2
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement `app/services/whisper.py`**

```python
from pathlib import Path

from faster_whisper import WhisperModel

_MODEL_CACHE: dict[str, WhisperModel] = {}


def _load_model(name: str) -> WhisperModel:
    if name not in _MODEL_CACHE:
        _MODEL_CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODEL_CACHE[name]


def transcribe(audio_path: Path, model_name: str = "small") -> str:
    model = _load_model(model_name)
    segments, _info = model.transcribe(str(audio_path), language=None, vad_filter=True)
    return "".join(seg.text for seg in segments).strip()
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add app/services/whisper.py tests/test_services_whisper.py
git commit -m "feat: faster-whisper transcription with model cache"
```

### Task 5.4: Transcript orchestrator service

**Files:**
- Create: `app/services/transcript.py`
- Create: `tests/test_services_transcript.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from unittest.mock import AsyncMock, patch


async def test_obtain_transcript_uses_subs_when_available(tmp_path):
    from app.services.transcript import obtain_transcript
    with (
        patch(
            "app.services.transcript.fetch_subtitles",
            AsyncMock(return_value=("subs text", "manual_subs")),
        ),
        patch("app.services.transcript.download_audio") as audio_mock,
    ):
        text, source = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert text == "subs text"
    assert source.value == "manual_subs"
    audio_mock.assert_not_called()


async def test_obtain_transcript_falls_back_to_whisper(tmp_path):
    from app.services.transcript import obtain_transcript
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.transcribe", return_value="whispered"),
    ):
        text, source = await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert text == "whispered"
    assert source.value == "whisper"


async def test_obtain_transcript_deletes_audio_after_whisper(tmp_path):
    from app.services.transcript import obtain_transcript
    fake_audio = tmp_path / "x.m4a"
    fake_audio.write_bytes(b"data")
    with (
        patch("app.services.transcript.fetch_subtitles", AsyncMock(return_value=None)),
        patch("app.services.transcript.download_audio", AsyncMock(return_value=fake_audio)),
        patch("app.services.transcript.transcribe", return_value="whispered"),
    ):
        await obtain_transcript(
            url="https://youtu.be/x",
            video_id="x",
            audio_dir=tmp_path,
            cookies_path=None,
            whisper_model="small",
        )
    assert not fake_audio.exists()
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement `app/services/transcript.py`**

```python
import asyncio
from pathlib import Path

from app.models import TranscriptSource
from app.services.whisper import transcribe
from app.services.youtube import download_audio, fetch_subtitles


async def obtain_transcript(
    *,
    url: str,
    video_id: str,
    audio_dir: Path,
    cookies_path: Path | None,
    whisper_model: str,
) -> tuple[str, TranscriptSource]:
    subs = await fetch_subtitles(url, cookies_path=cookies_path)
    if subs is not None:
        text, source = subs
        return text, TranscriptSource(source)

    audio_path = await download_audio(url, video_id, audio_dir, cookies_path=cookies_path)
    try:
        text = await asyncio.to_thread(transcribe, audio_path, whisper_model)
    finally:
        if audio_path.exists():
            audio_path.unlink()
    return text, TranscriptSource.WHISPER
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add app/services/transcript.py tests/test_services_transcript.py
git commit -m "feat: transcript orchestrator (subs > auto > whisper)"
```

---

## Phase 6: LLM summarization

### Task 6.1: Summarizer service (single-shot + map-reduce)

**Files:**
- Create: `app/services/summarizer.py`
- Create: `tests/test_services_summarizer.py`

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import AsyncMock, MagicMock, patch


def _completion_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.message.content = text
    response = MagicMock()
    response.choices = [msg]
    return response


async def test_summarize_single_shot_when_fits(tmp_path):
    from app.services.summarizer import summarize
    transcript = "short transcript"

    with (
        patch("app.services.summarizer.litellm.acompletion",
              AsyncMock(return_value=_completion_response("the summary"))),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.summarizer.litellm.get_max_tokens", return_value=8000),
    ):
        result = await summarize(
            transcript=transcript,
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
        )
    assert result == "the summary"


async def test_summarize_map_reduce_when_too_large():
    from app.services.summarizer import summarize
    big = " ".join(["word"] * 100_000)

    calls = {"n": 0, "messages": []}

    async def fake_completion(**kwargs):
        calls["n"] += 1
        calls["messages"].append(kwargs["messages"])
        return _completion_response(f"chunk-{calls['n']}")

    def fake_token_counter(*, model: str, text: str) -> int:
        return len(text.split())

    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", side_effect=fake_token_counter),
        patch("app.services.summarizer.litellm.get_max_tokens", return_value=2000),
    ):
        result = await summarize(
            transcript=big,
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
        )
    assert calls["n"] >= 2
    assert "chunk" in result.lower() or len(result) > 0


async def test_summarize_passes_base_url_when_set():
    from app.services.summarizer import summarize
    captured = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return _completion_response("x")

    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.summarizer.litellm.get_max_tokens", return_value=8000),
    ):
        await summarize(
            transcript="t",
            model="openai/gpt-4o",
            api_key="k",
            base_url="https://my.proxy/v1",
        )
    assert captured["api_key"] == "k"
    assert captured["api_base"] == "https://my.proxy/v1"
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement `app/services/summarizer.py`**

```python
from typing import Any

import litellm

SYSTEM_PROMPT = (
    "You are a careful summarizer. Produce a clear, structured summary of the "
    "following YouTube transcript. Use Markdown. Start with a one-paragraph "
    "TL;DR, then key points as bullets, then notable quotes if any."
)

REDUCE_SYSTEM_PROMPT = (
    "You are merging several partial summaries of a single YouTube transcript "
    "into one cohesive Markdown summary. Preserve the structure: TL;DR, key "
    "points, notable quotes."
)


def _split_into_chunks(transcript: str, model: str, target_tokens: int) -> list[str]:
    words = transcript.split()
    if not words:
        return []
    # Initial estimate: word/token ratio of ~0.75 for English. We refine by checking.
    approx_words_per_chunk = max(int(target_tokens * 0.6), 100)
    chunks: list[str] = []
    i = 0
    while i < len(words):
        end = min(i + approx_words_per_chunk, len(words))
        chunk = " ".join(words[i:end])
        # If chunk still too big per real token count, shrink.
        while litellm.token_counter(model=model, text=chunk) > target_tokens and end - i > 1:
            end = i + max((end - i) // 2, 1)
            chunk = " ".join(words[i:end])
        chunks.append(chunk)
        i = end
    return chunks


async def _completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    api_key: str,
    base_url: str | None,
) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
    }
    if base_url:
        kwargs["api_base"] = base_url
    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content or ""


async def summarize(
    *,
    transcript: str,
    model: str,
    api_key: str,
    base_url: str | None,
) -> str:
    max_tokens = litellm.get_max_tokens(model) or 8000
    # Reserve ~25% of context for the prompt + output.
    budget = int(max_tokens * 0.7)
    transcript_tokens = litellm.token_counter(model=model, text=transcript)

    if transcript_tokens <= budget:
        return await _completion(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            api_key=api_key,
            base_url=base_url,
        )

    # Map-reduce
    chunks = _split_into_chunks(transcript, model, target_tokens=budget // 2)
    partials: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        part = await _completion(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Part {idx} of {len(chunks)}:\n\n{chunk}",
                },
            ],
            api_key=api_key,
            base_url=base_url,
        )
        partials.append(part)

    return await _completion(
        model=model,
        messages=[
            {"role": "system", "content": REDUCE_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n---\n\n".join(partials)},
        ],
        api_key=api_key,
        base_url=base_url,
    )
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add app/services/summarizer.py tests/test_services_summarizer.py
git commit -m "feat: LiteLLM summarizer with map-reduce fallback"
```

### Task 6.2: Real pipeline (replace stub in `app/pipeline.py`)

**Files:**
- Modify: `app/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
from collections.abc import Awaitable
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import Config
from app.models import TranscriptSource
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo


async def test_pipeline_writes_transcript_and_summary(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    await videos_repo.upsert_metadata(
        db, video_id="v1", url="https://youtu.be/v1", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    await settings_repo.set(db, "llm_api_key", "key")
    await settings_repo.set(db, "whisper_model", "small")

    steps: list[str] = []

    async def set_step(s: str) -> None:
        steps.append(s)

    with (
        patch(
            "app.pipeline.obtain_transcript",
            AsyncMock(return_value=("the transcript", TranscriptSource.AUTO_SUBS)),
        ),
        patch(
            "app.pipeline.summarize",
            AsyncMock(return_value="THE SUMMARY"),
        ),
    ):
        from app.pipeline import process_video
        await process_video(db, config, "v1", set_step)

    v = await videos_repo.get(db, "v1")
    assert v is not None
    assert v.transcript == "the transcript"
    assert v.transcript_source is TranscriptSource.AUTO_SUBS
    assert v.summary == "THE SUMMARY"
    assert v.summary_model == "openai/gpt-4o"
    assert any("transcript" in s.lower() for s in steps)
    assert any("summary" in s.lower() or "summari" in s.lower() for s in steps)


async def test_pipeline_raises_when_llm_settings_missing(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )

    async def set_step(s: str) -> None:
        pass

    from app.pipeline import process_video
    import pytest
    with pytest.raises(RuntimeError, match="LLM"):
        await process_video(db, config, "v1", set_step)
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement real `app/pipeline.py`**

Replace contents:
```python
from collections.abc import Awaitable, Callable

import aiosqlite

from app.config import Config
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services.summarizer import summarize
from app.services.transcript import obtain_transcript


async def process_video(
    db: aiosqlite.Connection,
    config: Config,
    video_id: str,
    set_step: Callable[[str], Awaitable[None]],
) -> None:
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise RuntimeError(f"Video {video_id} not found")

    settings = await settings_repo.get_all(db)
    model = settings.get("llm_model")
    api_key = settings.get("llm_api_key")
    if not model or not api_key:
        raise RuntimeError("LLM model or API key not configured. Open Settings.")
    base_url = settings.get("llm_base_url")
    whisper_model = settings.get("whisper_model", "small")

    cookies = config.cookies_path if config.cookies_path.exists() else None

    await set_step("fetching transcript")
    text, source = await obtain_transcript(
        url=video.url,
        video_id=video_id,
        audio_dir=config.audio_dir,
        cookies_path=cookies,
        whisper_model=whisper_model,
    )
    await videos_repo.set_transcript(db, video_id, text, source)

    await set_step("summarizing")
    summary = await summarize(
        transcript=text,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )
    await videos_repo.set_summary(db, video_id, summary, model)
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add app/pipeline.py tests/test_pipeline.py
git commit -m "feat: real pipeline (transcript + summary persistence)"
```

---

## Phase 7: Detail page & Markdown permalink

### Task 7.1: Detail page route + template

**Files:**
- Create: `app/templates/video_detail.html`
- Modify: `app/routes/videos.py` (add routes)
- Modify: `tests/test_routes_videos.py`

- [ ] **Step 1: Add failing tests**

```python
def test_video_detail_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="MyTitle",
                description="d", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(app.state.db, "v1", "## TL;DR\nshort", "model")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1")
    assert resp.status_code == 200
    assert "MyTitle" in resp.text
    assert "TL;DR" in resp.text


def test_video_detail_404_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v/nope")
    assert resp.status_code == 404


def test_video_markdown_permalink(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="https://youtu.be/v1",
                title="MyTitle", description="d",
                thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "v1", "the transcript", TranscriptSource.MANUAL_SUBS,
            )
            await videos_repo.set_summary(app.state.db, "v1", "## TL;DR\nshort", "model")
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/v/v1.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    body = resp.text
    assert "# MyTitle" in body
    assert "## Summary" in body
    assert "## TL;DR" in body
    assert "## Transcript" in body
    assert "the transcript" in body
    assert "https://youtu.be/v1" in body
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Add `markdown` dependency for rendering**

Append to `pyproject.toml` `dependencies`:
```
"markdown-it-py>=3.0",
```

Then `pip install -e ".[dev]"`.

- [ ] **Step 4: Create `app/templates/video_detail.html`**

```html
{% extends "base.html" %}
{% block title %}{{ video.title }} — yt-summary{% endblock %}
{% block content %}
<article class="video-detail">
  <header>
    <h1>{{ video.title }}</h1>
    <p><a href="{{ video.url }}" target="_blank" rel="noopener">Watch on YouTube</a> ·
       <a href="/v/{{ video.id }}.md">Markdown</a></p>
  </header>

  {% if video.thumbnail_path %}
    <img src="/thumbnails/{{ video.id }}.jpg" alt="">
  {% endif %}

  <section class="summary">
    <h2>Summary</h2>
    {% if video.summary %}
      {{ summary_html | safe }}
    {% else %}
      {% include "video_status.html" %}
    {% endif %}
  </section>

  {% if video.transcript %}
    <details class="transcript">
      <summary>Transcript ({{ video.transcript_source.value if video.transcript_source else 'unknown' }})</summary>
      <pre>{{ video.transcript }}</pre>
    </details>
  {% endif %}

  {% if video.summary %}
    <section class="chat">
      <h2>Chat</h2>
      <div id="chat-history">
        {% for msg in chat_history %}
          {% include "_chat_message.html" %}
        {% endfor %}
      </div>
      <form
        hx-post="/v/{{ video.id }}/chat"
        hx-target="#chat-history"
        hx-swap="beforeend"
        hx-on::after-request="this.reset()"
      >
        <input name="content" placeholder="Ask about this video…" required>
        <button>Send</button>
      </form>
    </section>
  {% endif %}
</article>
{% endblock %}
```

- [ ] **Step 5: Create `app/templates/_chat_message.html`**

```html
<div class="chat-msg chat-msg-{{ msg.role }}">
  <strong>{{ msg.role }}:</strong>
  <div class="chat-content">{{ msg.content }}</div>
</div>
```

- [ ] **Step 6: Add routes to `app/routes/videos.py`**

Append:
```python
from fastapi.responses import PlainTextResponse
from markdown_it import MarkdownIt
from app.repos import chat as chat_repo

_md = MarkdownIt()


@router.get("/v/{video_id}", response_class=HTMLResponse)
async def video_detail(
    video_id: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    summary_html = _md.render(video.summary) if video.summary else ""
    history = await chat_repo.history(db, video_id)
    job = await jobs_repo.latest_for_video(db, video_id)
    return templates.TemplateResponse(
        request,
        "video_detail.html",
        {
            "video": video,
            "summary_html": summary_html,
            "chat_history": history,
            "job": job,
        },
    )


@router.get("/v/{video_id}.md")
async def video_markdown(
    video_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise HTTPException(404)
    parts: list[str] = [f"# {video.title}", "", f"Source: {video.url}", ""]
    if video.summary:
        parts += ["## Summary", "", video.summary, ""]
    if video.transcript:
        parts += ["## Transcript", "", video.transcript, ""]
    return PlainTextResponse("\n".join(parts), media_type="text/markdown; charset=utf-8")
```

- [ ] **Step 7: Run, verify pass**

- [ ] **Step 8: Commit**

```bash
git add app/templates/video_detail.html app/templates/_chat_message.html app/routes/videos.py tests/test_routes_videos.py pyproject.toml
git commit -m "feat: video detail page and Markdown permalink"
```

---

## Phase 8: Chat (SSE streaming)

### Task 8.1: Chat service with streaming

**Files:**
- Create: `app/services/chat.py`
- Create: `tests/test_services_chat.py`

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import AsyncMock, MagicMock, patch


def _stream_chunks(*texts: str):
    async def gen():
        for t in texts:
            choice = MagicMock()
            choice.delta.content = t
            chunk = MagicMock()
            chunk.choices = [choice]
            yield chunk
    return gen()


async def test_stream_reply_yields_token_strings():
    from app.services.chat import stream_reply

    with patch(
        "app.services.chat.litellm.acompletion",
        AsyncMock(return_value=_stream_chunks("Hello", " ", "world")),
    ):
        out: list[str] = []
        async for token in stream_reply(
            transcript="t",
            history=[],
            user_message="hi",
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
        ):
            out.append(token)
        assert "".join(out) == "Hello world"
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement `app/services/chat.py`**

```python
from collections.abc import AsyncIterator
from typing import Any

import litellm

from app.models import ChatMessage

SYSTEM_TEMPLATE = (
    "You are answering follow-up questions about a YouTube video. "
    "The full transcript is below. Answer accurately based on the transcript. "
    "If something is not in the transcript, say so.\n\n"
    "TRANSCRIPT:\n{transcript}"
)


async def stream_reply(
    *,
    transcript: str,
    history: list[ChatMessage],
    user_message: str,
    model: str,
    api_key: str,
    base_url: str | None,
) -> AsyncIterator[str]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_TEMPLATE.format(transcript=transcript)},
    ]
    for m in history:
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": user_message})

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "stream": True,
    }
    if base_url:
        kwargs["api_base"] = base_url

    response = await litellm.acompletion(**kwargs)
    async for chunk in response:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
git add app/services/chat.py tests/test_services_chat.py
git commit -m "feat: chat streaming service over LiteLLM"
```

### Task 8.2: Chat route

**Files:**
- Create: `app/routes/chat.py`
- Modify: `app/main.py`
- Create: `tests/test_routes_chat.py`

- [ ] **Step 1: Write the failing test**

```python
from collections.abc import AsyncIterator
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app


async def _fake_stream() -> AsyncIterator[str]:
    for s in ("Hello", " ", "user"):
        yield s


def test_post_chat_persists_and_streams(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.models import TranscriptSource
            from app.repos import settings as settings_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1", url="u", title="t",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_transcript(
                app.state.db, "v1", "transcript text", TranscriptSource.MANUAL_SUBS,
            )
            await settings_repo.set(app.state.db, "llm_model", "openai/gpt-4o")
            await settings_repo.set(app.state.db, "llm_api_key", "k")
        asyncio.get_event_loop().run_until_complete(setup())

        with patch("app.routes.chat.stream_reply", return_value=_fake_stream()):
            resp = client.post(
                "/v/v1/chat",
                data={"content": "what is this about?"},
                headers={"Accept": "text/event-stream"},
            )
        body = resp.text

    assert "Hello" in body
    # Confirm both messages were persisted
    import asyncio
    async def check():
        from app.repos import chat as chat_repo
        msgs = await chat_repo.history(app.state.db, "v1")
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[1].content == "Hello user"
    asyncio.get_event_loop().run_until_complete(check())
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Implement `app/routes/chat.py`**

```python
import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.main import get_db
from app.repos import chat as chat_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services.chat import stream_reply

router = APIRouter()


@router.post("/v/{video_id}/chat")
async def post_chat(
    video_id: str,
    request: Request,
    content: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    video = await videos_repo.get(db, video_id)
    if video is None or video.transcript is None:
        raise HTTPException(404, "Video or transcript not found")
    settings = await settings_repo.get_all(db)
    model = settings.get("llm_model")
    api_key = settings.get("llm_api_key")
    if not model or not api_key:
        raise HTTPException(400, "LLM not configured")

    history = await chat_repo.history(db, video_id)
    await chat_repo.append(db, video_id, "user", content)

    async def streamer():
        # Echo the user message immediately so the UI sees it.
        yield (
            f'<div class="chat-msg chat-msg-user">'
            f'<strong>user:</strong><div class="chat-content">{content}</div></div>'
        )
        yield '<div class="chat-msg chat-msg-assistant"><strong>assistant:</strong><div class="chat-content">'
        collected: list[str] = []
        async for token in stream_reply(
            transcript=video.transcript or "",
            history=history,
            user_message=content,
            model=model,
            api_key=api_key,
            base_url=settings.get("llm_base_url"),
        ):
            collected.append(token)
            yield token
        yield "</div></div>"
        await chat_repo.append(db, video_id, "assistant", "".join(collected))

    return StreamingResponse(streamer(), media_type="text/html; charset=utf-8")
```

- [ ] **Step 4: Wire router in `app/main.py`**

In `create_app`:
```python
from app.routes.chat import router as chat_router
app.include_router(chat_router)
```

- [ ] **Step 5: Run, verify pass**

- [ ] **Step 6: Commit**

```bash
git add app/routes/chat.py app/main.py tests/test_routes_chat.py
git commit -m "feat: chat endpoint with streaming HTML fragments"
```

---

## Phase 9: Settings

### Task 9.1: Curl parser service

**Files:**
- Create: `app/services/curl_parser.py`
- Create: `tests/test_services_curl_parser.py`
- Create: `tests/fixtures/curl_youtube.txt`

- [ ] **Step 1: Create the fixture**

`tests/fixtures/curl_youtube.txt`:
```
curl 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' \
  -H 'accept: text/html' \
  -H 'cookie: VISITOR_INFO1_LIVE=abc; YSC=def; LOGIN_INFO=xyz' \
  -H 'user-agent: Mozilla/5.0'
```

- [ ] **Step 2: Write the failing test**

```python
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_curl_extracts_cookies():
    from app.services.curl_parser import extract_cookies
    text = (FIXTURES / "curl_youtube.txt").read_text()
    cookies = extract_cookies(text)
    assert cookies == {
        "VISITOR_INFO1_LIVE": "abc",
        "YSC": "def",
        "LOGIN_INFO": "xyz",
    }


def test_parse_curl_handles_missing_cookie_header():
    from app.services.curl_parser import extract_cookies
    assert extract_cookies("curl 'https://x' -H 'accept: text/html'") == {}


def test_parse_curl_with_capital_cookie_header():
    from app.services.curl_parser import extract_cookies
    text = "curl 'https://x' -H 'Cookie: a=1; b=2'"
    assert extract_cookies(text) == {"a": "1", "b": "2"}


def test_write_netscape_cookie_file(tmp_path):
    from app.services.curl_parser import write_netscape_cookies
    target = tmp_path / "cookies.txt"
    write_netscape_cookies(
        {"a": "1", "b": "2"},
        domain=".youtube.com",
        target=target,
    )
    content = target.read_text()
    assert content.startswith("# Netscape HTTP Cookie File")
    assert "\t.youtube.com\t" in content
    # Two cookie rows
    rows = [l for l in content.splitlines() if not l.startswith("#") and l.strip()]
    assert len(rows) == 2
```

- [ ] **Step 3: Run, verify failure**

- [ ] **Step 4: Implement `app/services/curl_parser.py`**

```python
import re
import time
from pathlib import Path

_COOKIE_HEADER_RE = re.compile(
    r"-H\s+['\"](?:cookie|Cookie):\s*(?P<value>[^'\"]+)['\"]"
)


def extract_cookies(curl_text: str) -> dict[str, str]:
    match = _COOKIE_HEADER_RE.search(curl_text)
    if not match:
        return {}
    pairs = match.group("value").split(";")
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        out[name.strip()] = value.strip()
    return out


def write_netscape_cookies(
    cookies: dict[str, str], *, domain: str, target: Path
) -> None:
    expiry = int(time.time()) + 60 * 60 * 24 * 365  # 1 year
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value in cookies.items():
        lines.append("\t".join([domain, "TRUE", "/", "FALSE", str(expiry), name, value]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
```

- [ ] **Step 5: Run, verify pass**

- [ ] **Step 6: Commit**

```bash
git add app/services/curl_parser.py tests/test_services_curl_parser.py tests/fixtures/curl_youtube.txt
git commit -m "feat: curl parser → Netscape cookies.txt"
```

### Task 9.2: Settings routes & template

**Files:**
- Create: `app/templates/settings.html`
- Create: `app/routes/settings.py`
- Modify: `app/main.py`
- Create: `tests/test_routes_settings.py`

- [ ] **Step 1: Write failing tests**

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_get_settings_renders_form(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "llm_model" in resp.text
    assert "whisper_model" in resp.text


def test_post_settings_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/settings", data={
            "llm_model": "openai/gpt-4o",
            "llm_api_key": "k",
            "llm_base_url": "",
            "whisper_model": "small",
        })
    assert resp.status_code in (200, 303)
    import asyncio
    async def check():
        from app.repos import settings as settings_repo
        s = await settings_repo.get_all(app.state.db)
        assert s["llm_model"] == "openai/gpt-4o"
        assert s["whisper_model"] == "small"
    asyncio.get_event_loop().run_until_complete(check())


def test_post_youtube_curl_writes_cookie_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        curl_text = "curl 'https://www.youtube.com/' -H 'cookie: A=1; B=2'"
        resp = client.post("/settings/youtube-curl", data={"curl": curl_text})
    assert resp.status_code in (200, 303)
    cookies_file = tmp_path / "cookies.txt"
    assert cookies_file.exists()
    assert "A\t1" in cookies_file.read_text()
```

- [ ] **Step 2: Run, verify failure**

- [ ] **Step 3: Create `app/templates/settings.html`**

```html
{% extends "base.html" %}
{% block title %}Settings — yt-summary{% endblock %}
{% block content %}
<h1>Settings</h1>

<form method="post" action="/settings" class="settings-form">
  <label>
    LLM Model
    <input name="llm_model" value="{{ settings.get('llm_model', '') }}" placeholder="e.g. openai/gpt-4o, anthropic/claude-sonnet-4-6, ollama/llama3.1">
  </label>
  <label>
    LLM API Key
    <input name="llm_api_key" type="password" value="{{ settings.get('llm_api_key', '') }}">
  </label>
  <label>
    LLM Base URL (optional, for self-hosted OpenAI-compatible servers)
    <input name="llm_base_url" value="{{ settings.get('llm_base_url', '') }}">
  </label>
  <label>
    Whisper Model
    <select name="whisper_model">
      {% for m in ['tiny', 'base', 'small', 'medium', 'large-v3'] %}
        <option value="{{ m }}" {% if settings.get('whisper_model', 'small') == m %}selected{% endif %}>{{ m }}</option>
      {% endfor %}
    </select>
  </label>
  <button>Save</button>
</form>

<h2>YouTube cookies</h2>
<p>{% if has_cookies %}✓ cookies set <a href="/settings/youtube-curl/clear">clear</a>{% else %}no cookies set{% endif %}</p>
<form method="post" action="/settings/youtube-curl" class="curl-form">
  <label>
    Paste curl from DevTools (Network tab → right click on a youtube.com request → "Copy as cURL")
    <textarea name="curl" rows="6" placeholder="curl 'https://www.youtube.com/...' -H 'cookie: ...'"></textarea>
  </label>
  <button>Save cookies</button>
</form>
{% endblock %}
```

- [ ] **Step 4: Implement `app/routes/settings.py`**

```python
import aiosqlite
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.main import get_config, get_db
from app.repos import settings as settings_repo
from app.services.curl_parser import extract_cookies, write_netscape_cookies

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    config: Config = Depends(get_config),
):
    settings = await settings_repo.get_all(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings": settings, "has_cookies": config.cookies_path.exists()},
    )


@router.post("/settings")
async def save_settings(
    llm_model: str = Form(""),
    llm_api_key: str = Form(""),
    llm_base_url: str = Form(""),
    whisper_model: str = Form("small"),
    db: aiosqlite.Connection = Depends(get_db),
):
    for key, value in (
        ("llm_model", llm_model),
        ("llm_api_key", llm_api_key),
        ("llm_base_url", llm_base_url),
        ("whisper_model", whisper_model),
    ):
        if value:
            await settings_repo.set(db, key, value)
        else:
            await settings_repo.delete(db, key)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/youtube-curl")
async def save_curl(
    curl: str = Form(...),
    config: Config = Depends(get_config),
):
    cookies = extract_cookies(curl)
    if not cookies:
        return RedirectResponse("/settings", status_code=303)
    write_netscape_cookies(cookies, domain=".youtube.com", target=config.cookies_path)
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/youtube-curl/clear")
async def clear_curl(config: Config = Depends(get_config)):
    if config.cookies_path.exists():
        config.cookies_path.unlink()
    return RedirectResponse("/settings", status_code=303)
```

- [ ] **Step 5: Wire router in `app/main.py`**

```python
from app.routes.settings import router as settings_router
app.include_router(settings_router)
```

- [ ] **Step 6: Run, verify pass**

- [ ] **Step 7: Commit**

```bash
git add app/templates/settings.html app/routes/settings.py app/main.py tests/test_routes_settings.py
git commit -m "feat: settings page + curl-paste cookie capture"
```

### Task 9.3: Static asset bundling (download HTMX + Alpine + CSS)

**Files:**
- Modify: `app/static/htmx.min.js` (replace with real content)
- Modify: `app/static/alpine.min.js`
- Modify: `app/static/app.css`

- [ ] **Step 1: Download HTMX**

```bash
curl -L -o app/static/htmx.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
```

- [ ] **Step 2: Download Alpine.js**

```bash
curl -L -o app/static/alpine.min.js https://unpkg.com/alpinejs@3.14.7/dist/cdn.min.js
```

- [ ] **Step 3: Write minimal CSS**

`app/static/app.css`:
```css
:root { font-family: system-ui, sans-serif; --fg: #222; --muted: #666; --accent: #c00; }
body { margin: 0; color: var(--fg); }
header { display: flex; justify-content: space-between; align-items: center; padding: 1rem 2rem; border-bottom: 1px solid #eee; }
header .brand { font-weight: bold; text-decoration: none; color: inherit; }
header .gear { text-decoration: none; font-size: 1.5rem; }
main { max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
.submit-form, .search-form { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
.submit-form input, .search-form input { flex: 1; padding: 0.5rem; }
button { padding: 0.5rem 1rem; cursor: pointer; }
.video-card { display: grid; grid-template-columns: 160px 1fr; gap: 1rem; padding: 1rem 0; border-bottom: 1px solid #eee; }
.video-card img { width: 160px; height: auto; }
.video-card a { color: inherit; text-decoration: none; }
.video-card h3 { margin: 0; }
.status { color: var(--muted); }
.status-failed { color: var(--accent); }
.status-done { color: green; }
.video-detail img { max-width: 100%; }
.transcript pre { white-space: pre-wrap; max-height: 400px; overflow: auto; background: #f7f7f7; padding: 1rem; }
.chat-msg { padding: 0.5rem 0; }
.chat-msg-user { color: #036; }
.chat-msg-assistant { color: #060; }
.settings-form label, .curl-form label { display: block; margin-bottom: 1rem; }
.settings-form input, .settings-form select, .curl-form textarea { width: 100%; padding: 0.4rem; }
```

- [ ] **Step 4: Manual smoke test**

```bash
uvicorn app.main:app --reload
```
Open `http://localhost:8000`, configure settings, paste a YouTube URL, watch it process, click into the detail page, ask a chat question.

- [ ] **Step 5: Commit**

```bash
git add app/static
git commit -m "feat: bundle HTMX + Alpine + minimal CSS"
```

---

## Phase 10: Docker & GitHub Actions

### Task 10.1: Dockerfile

**Files:**
- Create: `docker/Dockerfile`
- Create: `docker/entrypoint.sh`

- [ ] **Step 1: Write `docker/Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    YTS_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app

RUN mkdir -p /data && chmod +x /app || true

EXPOSE 8000

VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Build the image locally to verify**

```bash
docker build -f docker/Dockerfile -t yt-summary:dev .
```
Expected: success.

- [ ] **Step 3: Smoke run**

```bash
docker run --rm -p 8000:8000 -v "$(pwd)/data:/data" yt-summary:dev
```
Open `http://localhost:8000`, confirm the home page renders.

- [ ] **Step 4: Commit**

```bash
git add docker/Dockerfile
git commit -m "feat: Dockerfile (single stage, slim base, ffmpeg)"
```

### Task 10.2: docker-compose.yml

**Files:**
- Create: `docker/docker-compose.yml`

- [ ] **Step 1: Write the file**

```yaml
services:
  yt-summary:
    image: ghcr.io/stefan-kp/yt-summary:latest
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: yt-summary
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

- [ ] **Step 2: Verify**

```bash
docker compose -f docker/docker-compose.yml config
```
Expected: prints resolved config without errors.

- [ ] **Step 3: Commit**

```bash
git add docker/docker-compose.yml
git commit -m "feat: docker-compose for local single-container deploy"
```

### Task 10.3: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - name: Install ffmpeg
        run: sudo apt-get update && sudo apt-get install -y ffmpeg
      - name: Install
        run: pip install -e ".[dev]"
      - name: Lint
        run: ruff check .
      - name: Type-check
        run: pyright
      - name: Test
        run: pytest -q
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: lint, type-check, and test on push/PR"
```

### Task 10.4: Release workflow (multi-arch image)

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: Release

on:
  push:
    tags: ["v*"]

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3
      - name: Set up Buildx
        uses: docker/setup-buildx-action@v3
      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Compute tags
        id: meta
        run: |
          REPO_LC=${GITHUB_REPOSITORY,,}
          VERSION=${GITHUB_REF_NAME}
          echo "tags=ghcr.io/${REPO_LC}:${VERSION},ghcr.io/${REPO_LC}:latest" >> "$GITHUB_OUTPUT"
      - name: Build and push (linux/amd64, linux/arm64)
        uses: docker/build-push-action@v6
        with:
          context: .
          file: docker/Dockerfile
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: multi-arch release workflow (amd64 + arm64) to GHCR"
```

### Task 10.5: Final smoke test

- [ ] **Step 1: Push to GitHub**

```bash
git push origin main
```

- [ ] **Step 2: Confirm CI passes** on GitHub Actions tab.

- [ ] **Step 3: Create the first release tag**

```bash
git tag v0.1.0
git push origin v0.1.0
```

Expected: Release workflow runs and pushes `ghcr.io/stefan-kp/yt-summary:0.1.0` and `:latest`.

- [ ] **Step 4: On the Pi (manual), pull and run**

```bash
docker compose -f docker/docker-compose.yml pull
docker compose -f docker/docker-compose.yml up -d
```

Open `http://<pi-ip>:8000`, configure settings, summarize a video.

---

## Self-Review

**Spec coverage check:**
- Self-hosted Docker, Mac + Pi → Phase 10 (multi-arch).
- yt-dlp metadata + thumbnail + description → Tasks 3.1, 3.2, 3.5.
- Subtitles preferred, then auto-captions, then Whisper → Task 5.4 orchestrator.
- faster-whisper, default `small`, configurable → Tasks 5.3, 9.2.
- LiteLLM with map-reduce → Task 6.1.
- Per-video chat with transcript context, SSE-ish streaming → Tasks 8.1, 8.2.
- Public Markdown permalink → Task 7.1.
- Search by title + description (FTS5) → Task 2.4 + 3.4.
- Settings: model, key, base URL, whisper model, curl-paste cookies → Tasks 9.1, 9.2.
- SQLite-backed job queue, single worker, no Redis → Tasks 2.5, 4.1, 4.2.
- Orphaned-job recovery on restart → Task 2.5 (`reset_orphaned_running`) + Task 3.3 wiring.
- GitHub Actions for CI + multi-arch release → Tasks 10.3, 10.4.

**Placeholder scan:** No "TBD"/"TODO"/"add appropriate error handling" in tasks. All steps have concrete code or commands.

**Type consistency:** `TranscriptSource`, `JobState`, `Video`, `Job`, `ChatMessage` are defined once in Task 2.3 and used consistently. Repo function names (`upsert_metadata`, `set_transcript`, `set_summary`, `claim_next`, `complete`, `fail`, `set_step`, `latest_for_video`, `append`, `history`) are used identically wherever they're referenced.

One callout: `Worker` in Task 4.1 takes a `process_video` callable; Task 4.2 wires the real implementation from `app.pipeline`. The signature `(db, config, video_id, set_step)` matches between definition (Task 4.1) and consumer (Task 6.2). ✓

No gaps detected.
