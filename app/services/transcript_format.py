"""Group raw (start_seconds, text) tuples into Markdown-renderable
blocks with a configurable gap heuristic.

VTT cues land every 2-5 seconds and Whisper segments every 5-30
seconds — both too granular to render one-line-per-cue. We collapse
adjacent cues into paragraphs whenever the gap between them is small
(< gap_s), and start a fresh paragraph (with a leading timestamp)
when the gap is bigger.

Output is a list of {"start": float, "text": str} dicts, JSON-
serialisable for storage in transcript_segments and easy to feed
to the Jinja template.
"""

from __future__ import annotations


def group_segments(
    segments: list[tuple[float, str]],
    *,
    gap_s: float = 8.0,
) -> list[dict]:
    """Collapse adjacent cues into paragraph-shaped blocks.

    Parameters
    ----------
    segments : list of (start_seconds, text)
        Raw cues from VTT or Whisper.
    gap_s : float
        Maximum gap between consecutive cue starts before a new
        paragraph is begun. Default 8s. Lower → more frequent
        timestamps; higher → longer paragraphs.

    Returns a list of {"start": float, "text": str}. The "start"
    is the start of the first cue in that paragraph; "text" is
    the concatenation of all cues in it, separated by spaces.
    """
    if not segments:
        return []

    out: list[dict] = []
    block_start, block_text = segments[0]
    block_parts = [block_text]
    last_start = block_start

    for start, text in segments[1:]:
        if start - last_start >= gap_s:
            # Flush the current block and start a new one.
            out.append({
                "start": block_start,
                "text": " ".join(block_parts).strip(),
            })
            block_start = start
            block_parts = [text]
        else:
            block_parts.append(text)
        last_start = start

    # Last block
    out.append({
        "start": block_start,
        "text": " ".join(block_parts).strip(),
    })
    return out


def format_timestamp(seconds: float, *, total_duration_s: float | None = None) -> str:
    """Format `seconds` as MM:SS or HH:MM:SS.

    Picks 3-tuple format if either `seconds` or `total_duration_s`
    is ≥ 3600 — keeps timestamps consistent within one transcript
    so a 90-minute video doesn't mix [05:30] and [01:30:00].
    """
    use_hours = seconds >= 3600 or (
        total_duration_s is not None and total_duration_s >= 3600
    )
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if use_hours:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"
