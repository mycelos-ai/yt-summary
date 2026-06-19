"""Parse the `{summary, highlights[]}` JSON envelope returned by the
summarizer LLM call.

Robust to:
- Code-fence-wrapped JSON (``` ```json … ``` ```)
- Plain JSON inline
- Free-text fallback (no JSON at all): returns (raw_text, None) so the
  pipeline falls back to the legacy "just store the summary" path.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)


# JSON allows a backslash only before " \ / b f n r t u. LLMs sometimes
# escape other characters — most commonly typographic quotes (\“ \”) — which
# is illegal JSON and makes json.loads raise. This strips a backslash that
# precedes any non-legal escape character, leaving the character itself.
_ILLEGAL_ESCAPE = re.compile(r'\\([^"\\/bfnrtu])')


def _loads_lenient(blob: str):
    """json.loads, retried once with illegal backslash-escapes repaired.

    Returns the parsed object, or raises json.JSONDecodeError if it is still
    invalid after the repair (caller treats that as the legacy fallback)."""
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        repaired = _ILLEGAL_ESCAPE.sub(r"\1", blob)
        return json.loads(repaired)

_MAX_HIGHLIGHT_TEXT = 400  # chars; anything longer is suspicious

HIGHLIGHTS_SCHEMA_HINT = """\
Return your answer as a single JSON object with this exact shape:

{
  "summary": "<the full markdown summary>",
  "image_query": "<2-4 English keywords for a fitting stock photo>",
  "highlights": [
    {"text": "<one concrete noteworthy point, <40 words>",
     "rank": <integer 1..5, 1 = most noteworthy>,
     "reason": "<one short sentence on why this matters>"},
    ...
  ]
}

Rules for "image_query":
- 2 to 4 concrete, visual English keywords describing the main topic,
  suitable for a stock-photo search (e.g. "data center servers",
  "wind turbines field"). Avoid abstract words and proper nouns that
  won't match stock libraries. Omit the field only if nothing visual
  fits.

Rules for "highlights":
- 3 to 5 entries is typical. If nothing in the content is genuinely
  worth surfacing, return [] (empty list). Silence is better than
  filler.
- Each "text" should be a self-contained statement readable out of
  context — not "this video discusses X" but "X claims Y".
- Use the interest-profile context (if provided) to decide what counts
  as noteworthy for this reader.
"""


_CODE_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.MULTILINE)


def _extract_json_blob(raw: str) -> str | None:
    """Find the first JSON-looking blob in raw text.

    Tries: code-fence first, then a brace-balanced first-object scan.
    The brace scan is string-aware — it ignores '{' and '}' inside JSON
    string literals (with proper handling of backslash escapes), so
    summaries containing braces in prose or code samples don't trip
    the boundary detection.
    """
    m = _CODE_FENCE.search(raw)
    if m:
        return m.group(1).strip()
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def _validate_highlight(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    text = entry.get("text")
    rank = entry.get("rank")
    reason = entry.get("reason", "")
    if not isinstance(text, str) or not text.strip():
        return None
    if len(text) > _MAX_HIGHLIGHT_TEXT:
        return None
    if isinstance(rank, bool) or not isinstance(rank, int):
        return None
    rank = max(1, min(5, rank))
    if not isinstance(reason, str):
        reason = ""
    return {"text": text.strip(), "rank": rank, "reason": reason.strip()}


def parse_summary_payload(raw: str) -> tuple[str, list[dict] | None]:
    """Parse the LLM's response into (summary_markdown, highlights_or_none).

    Returns:
      (summary, highlights) when JSON shape is valid; highlights may be
        an empty list.
      (raw, None) when the response is not parseable as the expected
        JSON envelope — caller treats this as the legacy "just summary"
        path, NULL highlights in DB.
    """
    blob = _extract_json_blob(raw)
    if blob is None:
        return (raw, None)
    try:
        payload = _loads_lenient(blob)
    except json.JSONDecodeError:
        return (raw, None)
    if not isinstance(payload, dict):
        return (raw, None)
    summary = payload.get("summary")
    highlights_raw = payload.get("highlights")
    if not isinstance(summary, str):
        return (raw, None)
    if not isinstance(highlights_raw, list):
        # Summary parsed but highlights malformed → keep summary, drop
        # highlights silently (NULL in DB).
        log.info("summary JSON missing 'highlights' list; treating as NULL")
        return (summary, None)
    highlights = [
        v for v in (_validate_highlight(e) for e in highlights_raw) if v
    ]
    return (summary, highlights)


def parse_image_query(raw: str) -> str | None:
    """Pull the optional `image_query` string out of the LLM envelope.

    Tolerant: returns None when the envelope is unparseable, the field
    is absent, blank, or not a string. Never raises — image queries are
    cosmetic.
    """
    blob = _extract_json_blob(raw)
    if blob is None:
        return None
    try:
        payload = _loads_lenient(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("image_query")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
