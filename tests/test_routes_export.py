"""Route tests for Part A — Markdown/JSON export (per-item + bulk ZIP).

No live network/LLM; we seed videos directly and assert on the rendered
files and ZIP structure.
"""

import asyncio
import io
import json
import zipfile

from fastapi.testclient import TestClient

from app.main import create_app


def _seed(app, *, vid, title="T", url=None, summary="## TL;DR\nhi",
          user_id=1, kind=None):
    from app.models import VideoKind
    from app.repos import videos as videos_repo
    url = url or f"https://youtu.be/{vid}"
    k = kind or VideoKind.YOUTUBE

    async def setup():
        await videos_repo.upsert_metadata(
            app.state.db, video_id=vid, url=url, title=title,
            description="d", thumbnail_path=None, duration_seconds=120,
            user_id=user_id, kind=k,
        )
        if summary is not None:
            await videos_repo.set_summary(
                app.state.db, vid, summary, "anthropic/claude-sonnet-4-6",
            )
    asyncio.get_event_loop().run_until_complete(setup())


# ----------------------------------------------------- per-item (web)

def test_export_item_md_returns_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="e1", title="Hello World")
        resp = client.get("/v/e1/export.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.text.startswith("---\n")
    assert 'title: "Hello World"' in resp.text
    assert "# Hello World" in resp.text


def test_export_item_md_transcript_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="e2")

        async def add_transcript():
            from app.models import TranscriptSource
            from app.repos import videos as videos_repo
            await videos_repo.set_transcript(
                app.state.db, "e2", "the full transcript text",
                TranscriptSource.AUTO_SUBS, language="en",
            )
        asyncio.get_event_loop().run_until_complete(add_transcript())
        without = client.get("/v/e2/export.md")
        with_t = client.get("/v/e2/export.md?transcript=1")
    assert "the full transcript text" not in without.text
    assert "## Transcript" in with_t.text
    assert "the full transcript text" in with_t.text


def test_export_item_json(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="e3", title="JSON Me")
        resp = client.get("/v/e3/export.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    doc = resp.json()
    assert doc["title"] == "JSON Me"
    assert doc["summary"].startswith("## TL;DR")
    assert "transcript" not in doc


def test_export_item_404_for_foreign_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="e4", user_id=2)  # belongs to another profile
        resp = client.get("/v/e4/export.md")
    assert resp.status_code == 404


# ------------------------------------------------------ per-item (api)

def test_api_export_item_requires_no_key_when_unconfigured(tmp_path, monkeypatch):
    """With no API key set, auth is disabled (default user) so the API
    export works — same posture as the rest of /api/v1."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="a1", title="Api Item")
        resp = client.get("/api/v1/videos/a1/export?format=json")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Api Item"


def test_api_export_item_md_default_format(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="a2", title="Md Default")
        resp = client.get("/api/v1/videos/a2/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert 'title: "Md Default"' in resp.text


# ----------------------------------------------------------- bulk zip

def _open_zip(resp):
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_export_zip_whole_library(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="z1", title="One")
        _seed(app, vid="z2", title="Two")
        resp = client.get("/export.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    zf = _open_zip(resp)
    names = set(zf.namelist())
    assert "manifest.json" in names
    assert len([n for n in names if n.endswith(".md")]) == 2
    manifest = json.loads(zf.read("manifest.json"))
    assert {e["title"] for e in manifest} == {"One", "Two"}


def test_export_zip_filtered_by_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    from app.models import VideoKind
    with TestClient(app) as client:
        _seed(app, vid="yt1", title="A YouTube", kind=VideoKind.YOUTUBE)
        _seed(app, vid="web1", title="A Web", kind=VideoKind.WEB,
              url="https://example.com/x")
        resp = client.get("/export.zip?kind=web")
    assert resp.status_code == 200
    manifest = json.loads(_open_zip(resp).read("manifest.json"))
    assert {e["title"] for e in manifest} == {"A Web"}


def test_export_zip_rejects_bad_date(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="d1")
        resp = client.get("/export.zip?since=not-a-date")
    assert resp.status_code == 400


def test_export_zip_empty_filter_params_mean_no_filter(tmp_path, monkeypatch):
    """The settings Export form submits all filter fields, empty ones
    included (kind=&tag=&since=&until=). Empty must mean 'no filter',
    not a 422 pattern-mismatch."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="ef1", title="One")
        _seed(app, vid="ef2", title="Two")
        resp = client.get(
            "/export.zip?format=md&kind=&tag=&since=&until=&playlist_id=",
        )
    assert resp.status_code == 200
    manifest = json.loads(_open_zip(resp).read("manifest.json"))
    assert len(manifest) == 2


def test_export_zip_json_format(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        _seed(app, vid="j1", title="JsonBulk")
        resp = client.get("/export.zip?format=json")
    zf = _open_zip(resp)
    json_files = [n for n in zf.namelist()
                  if n.endswith(".json") and n != "manifest.json"]
    assert len(json_files) == 1
    assert json.loads(zf.read(json_files[0]))["title"] == "JsonBulk"
