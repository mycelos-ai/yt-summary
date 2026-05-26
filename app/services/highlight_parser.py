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

_MAX_HIGHLIGHT_TEXT = 400  # chars; anything longer is suspicious

HIGHLIGHTS_SCHEMA_HINT = """\
Return your answer as a single JSON object with this exact shape:

{
  "summary": "<the full markdown summary>",
  "highlights": [
    {"text": "<one concrete noteworthy point, <40 words>",
     "rank": <integer 1..5, 1 = most noteworthy>,
     "reason": "<one short sentence on why this matters>"},
    ...
  ]
}

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
    """
    m = _CODE_FENCE.search(raw)
    if m:
        return m.group(1).strip()
    # Fallback: find the first '{' and the matching '}' by brace depth.
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(raw)):
        ch = raw[i]
        if ch == "{":
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
    if not isinstance(rank, int):
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
        payload = json.loads(blob)
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
