"""Unit tests for the pure export builders (services/export.py).

These are pure text/dict functions — no DB, no network. They turn a
Video (+ its tags/playlists) into Obsidian-friendly Markdown or a
self-contained JSON document, and derive a stable export filename.
"""

from datetime import UTC, datetime

from app.models import Video, VideoKind


def _video(**kw) -> Video:
    ts = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    v = Video(
        id="1:abc12345678",
        url="https://youtu.be/abc12345678",
        title="My Great Video",
        description="d",
        thumbnail_path=None,
        duration_seconds=3841,
        transcript="hello world transcript",
        transcript_source=None,
        summary="## TL;DR\nA point at [01:34](#t=94) and another.",
        summary_model="anthropic/claude-sonnet-4-6",
        created_at=ts,
        updated_at=ts,
        kind=VideoKind.YOUTUBE,
        user_id=1,
        transcript_segments=None,
        youtube_id="abc12345678",
        source_language="en",
        summary_language=None,
        transcript_language=None,
        highlights_json=None,
        archived_at=None,
        image_query=None,
        related_links_json=None,
    )
    for k, val in kw.items():
        setattr(v, k, val)
    return v


# ----------------------------------------------------------- filename

def test_export_filename_shape():
    from app.services.export import export_filename
    name = export_filename(_video())
    assert name == "2026-06-10-my-great-video-abc12345678.md"


def test_export_filename_slugs_unicode_and_punctuation():
    from app.services.export import export_filename
    v = _video(title="Café  &  Crème: a Tëst!", id="1:zzz99999999")
    name = export_filename(v)
    # ASCII-folded, lowercased, single dashes, short-id suffix.
    assert name == "2026-06-10-cafe-creme-a-test-zzz99999999.md"


def test_export_filename_unique_suffix_distinguishes_same_title():
    from app.services.export import export_filename
    a = export_filename(_video(title="Same", id="1:aaaaaaaaaaa"))
    b = export_filename(_video(title="Same", id="1:bbbbbbbbbbb"))
    assert a != b


# ------------------------------------------------ timestamp rewriting

def test_rewrite_timestamp_links_to_absolute_youtube():
    from app.services.export import rewrite_timestamp_links
    md = "See [01:34](#t=94) and [1:02:03](#t=3723)."
    out = rewrite_timestamp_links(md, youtube_id="abc12345678")
    assert "(#t=94)" not in out
    assert "https://youtube.com/watch?v=abc12345678&t=94s" in out
    assert "https://youtube.com/watch?v=abc12345678&t=3723s" in out
    # The visible label is preserved.
    assert "[01:34]" in out


def test_rewrite_timestamp_links_noop_without_youtube_id():
    from app.services.export import rewrite_timestamp_links
    md = "Web article, no [00:10](#t=10) links normally."
    out = rewrite_timestamp_links(md, youtube_id=None)
    assert out == md


# ------------------------------------------------------ markdown doc

def test_render_item_md_has_frontmatter_and_summary():
    from app.services.export import render_item_md
    out = render_item_md(
        _video(), tags=["ai", "agents"],
        playlists=["AI", "Long-form"],
    )
    assert out.startswith("---\n")
    assert 'title: "My Great Video"' in out
    assert "source_url: \"https://youtu.be/abc12345678\"" in out
    assert "kind: youtube" in out
    assert "created: 2026-06-10" in out
    assert 'summary_model: "anthropic/claude-sonnet-4-6"' in out
    assert "tags: [ai, agents]" in out
    assert "# My Great Video" in out
    # Summary body present, with timestamp rewritten to a deep link.
    assert "youtube.com/watch?v=abc12345678&t=94s" in out


def test_render_item_md_frontmatter_carries_identity():
    from app.services.export import SOURCE, render_item_md
    v = _video()
    out = render_item_md(v, tags=[], playlists=[])
    # An exported note must say what it is and where it came from,
    # so a re-import can match it to the existing item.
    assert f'id: "{v.id}"' in out
    assert f'source: "{SOURCE}"' in out
    assert "updated: 2026-06-10T12:00:00Z" in out


def test_render_item_md_transcript_opt_in():
    from app.services.export import render_item_md
    without = render_item_md(_video(), tags=[], playlists=[])
    assert "## Transcript" not in without
    with_t = render_item_md(
        _video(), tags=[], playlists=[], transcript=True,
    )
    assert "## Transcript" in with_t
    assert "hello world transcript" in with_t


def test_render_item_md_escapes_quotes_in_title_frontmatter():
    from app.services.export import render_item_md
    out = render_item_md(
        _video(title='He said "hi"'), tags=[], playlists=[],
    )
    # YAML double-quoted scalar: embedded quotes escaped with backslash.
    assert 'title: "He said \\"hi\\""' in out


# ---------------------------------------------------------- json doc

def test_render_item_json_is_self_contained():
    from app.services.export import render_item_json
    v = _video()
    doc = render_item_json(
        v, tags=["ai"], playlists=[("p1", "AI")],
        transcript=False, highlights=[{"text": "h", "rank": 5, "reason": "r"}],
        feedback=[{"id": 1, "sentiment": "interesting"}],
    )
    assert doc["id"] == v.id
    assert doc["title"] == v.title
    assert doc["url"] == v.url
    assert doc["summary"] == v.summary
    assert "transcript" not in doc  # opt-in
    assert doc["highlights"] == [{"text": "h", "rank": 5, "reason": "r"}]
    assert doc["feedback"] == [{"id": 1, "sentiment": "interesting"}]
    assert doc["tags"] == ["ai"]


def test_render_item_json_includes_transcript_when_opted_in():
    from app.services.export import render_item_json
    doc = render_item_json(
        _video(), tags=[], playlists=[], transcript=True,
        highlights=None, feedback=[],
    )
    assert doc["transcript"] == "hello world transcript"


def test_render_item_json_carries_identity():
    from app.services.export import SOURCE, render_item_json
    v = _video()
    doc = render_item_json(v, tags=[], playlists=[])
    assert doc["id"] == v.id
    assert doc["source"] == SOURCE
    assert doc["updated_at"] == "2026-06-10T12:00:00Z"


# ------------------------------------------------------------ okf doc

def test_render_item_okf_maps_the_okf_vocabulary():
    from app.services.export import SOURCE, render_item_okf
    v = _video()
    doc = render_item_okf(
        v, tags=["ai"], playlists=["AI"],
        highlights=[{"text": "h", "reason": "r"}],
    )
    # OKF's own field names, so a consumer needs no translation table.
    assert doc["type"] == "note"
    assert doc["title"] == v.title
    assert doc["description"] == v.description
    assert doc["resource"] == v.url
    assert doc["timestamp"] == "2026-06-10T12:00:00Z"
    assert doc["tags"] == ["ai"]
    # Identity.
    assert doc["id"] == v.id
    assert doc["source"] == SOURCE
    # yt-summary metadata.
    assert doc["kind"] == "youtube"
    assert doc["language"] == "en"
    assert doc["summary_model"] == "anthropic/claude-sonnet-4-6"
    assert doc["playlists"] == ["AI"]
    assert doc["duration_seconds"] == 3841
    assert doc["highlights"] == [{"text": "h", "reason": "r"}]


def test_render_item_okf_never_includes_the_transcript():
    from app.services.export import render_item_okf
    v = _video()
    assert v.transcript, "fixture must have a transcript for this to mean anything"
    doc = render_item_okf(v, tags=[], playlists=[])
    assert "transcript" not in doc
    assert v.transcript not in str(doc)


def test_render_item_okf_content_has_rewritten_timestamp_links():
    from app.services.export import render_item_okf
    doc = render_item_okf(_video(), tags=[], playlists=[])
    # Same treatment as the Markdown export: in-app links are useless
    # outside the app.
    assert "(#t=94)" not in doc["content"]
    assert "youtube.com/watch?v=abc12345678&t=94s" in doc["content"]


def test_render_item_okf_tolerates_missing_optional_fields():
    from app.services.export import render_item_okf
    v = _video(
        summary=None, summary_model=None, duration_seconds=None,
        source_language=None, summary_language=None,
    )
    doc = render_item_okf(v, tags=[], playlists=[])
    assert doc["content"] == ""
    assert doc["summary_model"] is None
    assert doc["duration_seconds"] is None
    assert doc["language"] is None
    assert doc["highlights"] == []


# ----------------------------------------------------------- bulk zip

def test_build_export_zip_md_has_manifest_and_one_file_per_item():
    import io
    import json
    import zipfile

    from app.services.export import build_export_zip
    a = _video(title="First", id="1:aaaaaaaaaaa", youtube_id="aaaaaaaaaaa")
    b = _video(title="Second", id="1:bbbbbbbbbbb", youtube_id="bbbbbbbbbbb")
    items = [
        {"video": a, "tags": ["x"], "playlists": ["P"], "feedback": [],
         "highlights": None},
        {"video": b, "tags": [], "playlists": [], "feedback": [],
         "highlights": None},
    ]
    raw = build_export_zip(items, fmt="md")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = set(zf.namelist())
    assert "manifest.json" in names
    # one .md per item, named by export_filename
    md_names = {n for n in names if n.endswith(".md")}
    assert len(md_names) == 2
    manifest = json.loads(zf.read("manifest.json"))
    assert len(manifest) == 2
    entry = next(e for e in manifest if e["id"] == a.id)
    assert entry["title"] == "First"
    assert entry["url"] == a.url
    assert entry["file"] in md_names


def test_build_export_zip_manifest_carries_source():
    import io
    import json
    import zipfile

    from app.services.export import SOURCE, build_export_zip
    items = [{"video": _video(), "tags": [], "playlists": []}]
    raw = build_export_zip(items, fmt="md")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest[0]["source"] == SOURCE
    assert manifest[0]["id"] == "1:abc12345678"


def test_build_export_zip_json_format_writes_json_files():
    import io
    import json
    import zipfile

    from app.services.export import build_export_zip
    a = _video(title="Solo", id="1:aaaaaaaaaaa")
    items = [{"video": a, "tags": [], "playlists": [], "feedback": [],
              "highlights": None}]
    raw = build_export_zip(items, fmt="json")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    json_names = [n for n in zf.namelist() if n.endswith(".json")
                  and n != "manifest.json"]
    assert len(json_names) == 1
    doc = json.loads(zf.read(json_names[0]))
    assert doc["title"] == "Solo"
