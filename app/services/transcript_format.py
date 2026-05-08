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

    A speaker-marker cue (text starting with ">>") always begins a
    fresh block — YouTube uses ">>" for "next speaker" in its
    auto-captions, so paragraph splits on those produce a much more
    readable interview transcript than gap-only grouping.

    Returns a list of {"start": float, "text": str}. The "start"
    is the start of the first cue in that paragraph; "text" is
    the concatenation of all cues in it. The leading ">> " marker
    is stripped from the rendered text since it's redundant once
    the block break itself signals the speaker change.
    """
    if not segments:
        return []

    def _starts_speaker(text: str) -> bool:
        return text.lstrip().startswith(">>")

    def _strip_speaker(text: str) -> str:
        return text.lstrip().removeprefix(">>").lstrip()

    out: list[dict] = []
    first_start, first_text = segments[0]
    block_start: float = first_start
    first_clean = _strip_speaker(first_text) if _starts_speaker(first_text) else first_text
    block_parts: list[str] = [first_clean] if first_clean else []
    last_start = block_start

    def emit_block() -> None:
        # Helper to flush block_start/block_parts to `out`. Skips empty
        # blocks (e.g. a `>>` cue with no following text) so they
        # don't contribute a phantom timestamp.
        text = " ".join(block_parts).strip()
        if text:
            out.append({"start": block_start, "text": text})

    for start, text in segments[1:]:
        is_speaker = _starts_speaker(text)
        gap_break = start - last_start >= gap_s
        if is_speaker or gap_break:
            emit_block()
            block_start = start
            cleaned = _strip_speaker(text) if is_speaker else text
            block_parts = [cleaned] if cleaned else []
        else:
            if text:
                # If the current block is still empty (e.g. it was
                # opened by a bare `>>` marker with no following
                # content), advance block_start to this cue — that's
                # when the actual speech starts.
                if not block_parts:
                    block_start = start
                block_parts.append(text)
        last_start = start

    emit_block()
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
