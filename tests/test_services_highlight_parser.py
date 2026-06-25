from app.services.highlight_parser import (
    HIGHLIGHTS_SCHEMA_HINT,
    parse_image_query,
    parse_summary_payload,
)


def test_parses_summary_and_highlights():
    raw = """
    ```json
    {
      "summary": "## TL;DR\\nGreat video.",
      "highlights": [
        {"text": "Key insight A", "rank": 1, "reason": "novel claim"},
        {"text": "Key insight B", "rank": 2, "reason": "useful detail"}
      ]
    }
    ```
    """
    summary, highlights = parse_summary_payload(raw)
    assert summary.startswith("## TL;DR")
    assert len(highlights) == 2
    assert highlights[0]["rank"] == 1


def test_parses_inline_json_without_codefence():
    raw = '{"summary": "s", "highlights": []}'
    summary, highlights = parse_summary_payload(raw)
    assert summary == "s"
    assert highlights == []


def test_returns_summary_and_none_when_not_json():
    # Legacy / fallback: model returned plain markdown summary.
    raw = "## TL;DR\nSome summary text."
    summary, highlights = parse_summary_payload(raw)
    assert summary.startswith("## TL;DR")
    assert highlights is None


def test_drops_malformed_highlight_entries():
    raw = """{
      "summary": "s",
      "highlights": [
        {"text": "ok", "rank": 1, "reason": "good"},
        {"text": "", "rank": 1, "reason": "empty text"},
        {"text": "no rank"},
        "not even an object"
      ]
    }"""
    summary, highlights = parse_summary_payload(raw)
    assert summary == "s"
    assert highlights is not None
    assert len(highlights) == 1
    assert highlights[0]["text"] == "ok"


def test_clamps_rank_into_valid_range():
    raw = '{"summary":"s","highlights":[{"text":"x","rank":99,"reason":"y"}]}'
    _, highlights = parse_summary_payload(raw)
    assert highlights is not None
    assert highlights[0]["rank"] == 5


def test_drops_overlong_highlight_text():
    # Anything > 400 chars is suspicious — drop the entry.
    raw = (
        '{"summary":"s","highlights":[{"text":"' + "x" * 500 +
        '","rank":1,"reason":"y"}]}'
    )
    _, highlights = parse_summary_payload(raw)
    assert highlights == []


def test_schema_hint_constant_exists():
    assert "highlights" in HIGHLIGHTS_SCHEMA_HINT
    assert "summary" in HIGHLIGHTS_SCHEMA_HINT


def test_brace_inside_summary_string_does_not_truncate_payload():
    """A summary containing '}' must not cause the brace-balanced
    scanner to close the outer object early."""
    raw = (
        '{"summary": "the function foo() { return 1; }",'
        '"highlights": [{"text":"x","rank":1,"reason":"y"}]}'
    )
    summary, highlights = parse_summary_payload(raw)
    assert summary == "the function foo() { return 1; }"
    assert highlights is not None
    assert len(highlights) == 1


def test_escaped_quote_inside_summary_string():
    """The brace scanner must respect backslash-escaped quotes inside
    JSON string literals so it doesn't think the string ended early."""
    raw = (
        '{"summary": "she said \\"yes }\\" loudly",'
        '"highlights": []}'
    )
    summary, highlights = parse_summary_payload(raw)
    assert summary == 'she said "yes }" loudly'
    assert highlights == []


def test_rejects_bool_rank():
    """`rank: true` must be rejected, not silently clamped to 1.
    Python quirk: isinstance(True, int) is True, so a naive type
    check would let booleans through."""
    raw = '{"summary":"s","highlights":[{"text":"x","rank":true,"reason":"y"}]}'
    _, highlights = parse_summary_payload(raw)
    assert highlights == []


def test_parse_image_query_present():
    raw = '{"summary": "s", "highlights": [], "image_query": "solar panels"}'
    assert parse_image_query(raw) == "solar panels"


def test_parse_image_query_missing():
    raw = '{"summary": "s", "highlights": []}'
    assert parse_image_query(raw) is None


def test_parse_image_query_wrong_type():
    raw = '{"summary": "s", "image_query": 123}'
    assert parse_image_query(raw) is None


def test_parse_image_query_blank():
    raw = '{"summary": "s", "image_query": "   "}'
    assert parse_image_query(raw) is None


def test_parse_image_query_unparseable():
    assert parse_image_query("not json at all") is None


def test_parses_envelope_with_escaped_smart_quotes():
    """LLMs sometimes backslash-escape typographic quotes (\\“ \\”) inside
    the JSON envelope. That is illegal JSON (only \\" \\\\ \\n … are valid
    escapes), so a strict json.loads fails and the whole raw blob would
    wrongly surface as the summary. The parser must repair these illegal
    escapes and unwrap the envelope."""
    raw = r'''{"summary": "He cited \"Policy on the AI Exponential\" in his essay.", "highlights": [{"text": "A claim", "rank": 1, "reason": "why"}]}'''  # noqa: E501
    summary, highlights = parse_summary_payload(raw)
    # Must be the UNWRAPPED summary, not the raw JSON blob.
    assert summary.startswith("He cited")
    assert '"summary"' not in summary       # raw JSON did NOT leak through
    assert highlights == [{"text": "A claim", "rank": 1, "reason": "why"}]
