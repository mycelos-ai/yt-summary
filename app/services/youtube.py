import asyncio
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)

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
    tags: tuple[str, ...] = ()


def _base_opts(cookies_path: Path | None) -> dict[str, Any]:
    """Common yt-dlp options for every call we make.

    `remote_components: ["ejs:github"]` is what lets yt-dlp fetch the
    EJS challenge-solver script that decodes YouTube's signed
    n-parameter on stream URLs. Without it, even with Deno installed,
    yt-dlp falls back to storyboard images only and any download
    fails with `Requested format is not available`. The pip-installed
    yt-dlp is not the "official executable" yt-dlp's docs reference,
    so we have to opt in explicitly.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "remote_components": ["ejs:github"],
    }
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)
    return opts


def _extract_info(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts = _base_opts(cookies_path)
    opts["skip_download"] = True
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


def _pick_best_thumbnail(info: dict[str, Any]) -> str | None:
    """Pick the highest-resolution thumbnail yt-dlp surfaced.

    yt-dlp's top-level info["thumbnail"] is often hqdefault (480x360),
    which looks soft when scaled to the card width. The thumbnails list
    typically also has maxresdefault (1280x720) and sddefault (640x480).
    We pick the largest by width; fall back to any URL if width is
    missing; final fallback is info["thumbnail"].
    """
    thumbs = info.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        with_width = [
            t for t in thumbs
            if isinstance(t, dict) and t.get("url") and isinstance(t.get("width"), int)
        ]
        if with_width:
            best = max(with_width, key=lambda t: t["width"])
            return best["url"]
        # No width info → take the last entry (yt-dlp orders ascending in
        # most extractors, so the last is usually the largest).
        for t in reversed(thumbs):
            if isinstance(t, dict) and t.get("url"):
                return t["url"]
    return info.get("thumbnail")


async def fetch_metadata(url: str, cookies_path: Path | None) -> VideoMetadata:
    info = await asyncio.to_thread(_extract_info, url, cookies_path)
    raw_tags = info.get("tags") or []
    tags = tuple(t for t in raw_tags if isinstance(t, str) and t.strip())
    return VideoMetadata(
        id=info["id"],
        url=info.get("webpage_url", url),
        title=info.get("title", ""),
        description=info.get("description") or "",
        duration_seconds=info.get("duration"),
        thumbnail_url=_pick_best_thumbnail(info),
        tags=tags,
    )


async def download_thumbnail(url: str | None, target: Path) -> None:
    if not url:
        return
    async_target = anyio.Path(target)
    await async_target.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        await async_target.write_bytes(resp.content)


SubtitleSource = Literal["manual_subs", "auto_subs"]


def _extract_info_with_subs(url: str, cookies_path: Path | None) -> dict[str, Any]:
    opts = _base_opts(cookies_path)
    opts.update({
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "de"],
        "subtitlesformat": "vtt",
    })
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)  # type: ignore[return-value]


async def _download_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


_VTT_CUE_HEAD = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}"
)
# Inline word-timestamp tag emitted by YouTube auto-captions.
# Example: `released<00:00:01.040>` — the timestamp belongs to the
# WORD THAT FOLLOWS the tag, not the one that precedes it.
_VTT_TIMESTAMP_TAG = re.compile(r"<(\d{2}):(\d{2}):(\d{2})\.(\d{3})>")
# Other VTT styling tags: <c>, </c>, <c.colorE5E5E5>, etc.
_VTT_STYLE_TAG = re.compile(r"</?c(?:\.[^>]*)?>")


def _vtt_time_to_seconds(hh: str, mm: str, ss: str, ms: str) -> float:
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000


def _parse_cue_body(body: str) -> tuple[str, list[tuple[int, float]]]:
    """Parse the text payload of a single VTT cue.

    YouTube auto-caption cues look like:

        and<00:00:02.399><c> this</c><00:00:02.560><c> little</c>...

    Each `<HH:MM:SS.mmm>` marks the start time of the WORD THAT
    FOLLOWS it. We split the body around these markers to recover
    per-word timestamps, then strip the `<c>` styling tags from the
    text.

    Returns:
        (clean_text, word_starts)
      where word_starts is a list of (word_index, start_seconds)
      tuples — the indices refer to whitespace-split words of
      clean_text, NOT character offsets. Only words that had a
      preceding inline timestamp tag in the source appear; words
      that came before the first tag are omitted.

    HTML entities are decoded so YouTube's `&gt;&gt;` speaker
    markers don't leak through.
    """
    # Strip styling tags first; they carry no timing info and would
    # otherwise complicate the split below.
    body = _VTT_STYLE_TAG.sub("", body)
    # Split around timestamp tags. The capturing groups in the
    # pattern (hh, mm, ss, ms) appear in the resulting list, so we
    # walk it in 5-tuples: [text_before, hh, mm, ss, ms, text_after, ...]
    parts = _VTT_TIMESTAMP_TAG.split(body)
    pieces: list[str] = []
    word_starts: list[tuple[int, float]] = []
    # First piece is the text before any timestamp tag.
    head = html.unescape(parts[0])
    pieces.append(head)
    running_word_count = len(head.split())
    # Then groups of (hh, mm, ss, ms, text).
    for i in range(1, len(parts), 5):
        hh, mm, ss, ms, after = parts[i:i + 5]
        start = _vtt_time_to_seconds(hh, mm, ss, ms)
        word_starts.append((running_word_count, start))
        after_text = html.unescape(after)
        pieces.append(after_text)
        running_word_count += len(after_text.split())
    full_text = " ".join(p for p in (" ".join(pieces).split()) if p)
    return full_text, word_starts


def vtt_to_segments(vtt: str) -> list[tuple[float, str]]:
    """Parse VTT into a list of (start_seconds, text) cue tuples.

    YouTube's VTT files have one block per spoken phrase, each starting
    with an HH:MM:SS.mmm --> HH:MM:SS.mmm header followed by one or more
    lines of text.

    Three normalisations:

    1. HTML entities (`&gt;`, `&amp;`, ...) are decoded — YouTube wraps
       speaker markers `>>` as `&gt;&gt;` so they would otherwise leak
       into the rendered transcript as literal `&gt;&gt;`.

    2. Inline VTT styling tags (`<c>`, `</c>`) are stripped, but inline
       word-timestamp tags (`<00:00:01.040>`) are PARSED and used to
       fix up the start time of the trimmed tail (see #3).

    3. Auto-caption rolling-window duplication is collapsed by suffix
       trimming: if a new cue starts with the previous emitted text,
       only the new tail is kept. The trimmed tail's start time comes
       from the inline timestamp of its first word when available —
       without that fixup the entire transcript drifts earlier by
       however long the rolling window happened to be. Plain "exact
       equality" doesn't work because YouTube's auto-captions emit
       cumulative cues like:
            cue 1: "hello"
            cue 2: "hello world"
            cue 3: "hello world today"
       We want one entry per cue containing only the new words.

    Cue body lines that are whitespace-only (YouTube's auto-captions
    sometimes emit a single-space line as the first body line) are
    skipped without flushing the in-progress cue.
    """
    out: list[tuple[float, str]] = []
    last_text = ""
    current_start: float | None = None
    current_body_lines: list[str] = []
    # True while we're inside a cue (between its header and the next
    # blank line). YouTube sometimes emits cues whose first body line
    # is a single space, which we must NOT treat as the cue-terminating
    # blank line.
    in_cue = False

    def flush() -> None:
        nonlocal last_text, current_start, current_body_lines, in_cue
        if current_start is None or not current_body_lines:
            current_start = None
            current_body_lines = []
            in_cue = False
            return
        # Concatenate body lines preserving the inline timestamp tags,
        # then parse the combined body in one go.
        raw_body = " ".join(current_body_lines)
        text, word_starts = _parse_cue_body(raw_body)
        if text:
            # Rolling-window dedup: if the previous cue ended with
            # text we're about to emit, only keep the tail.
            trimmed, trim_offset = _trim_overlap_with_offset(
                prev=last_text, current=text,
            )
            if trimmed:
                # When a tail was trimmed from the front of the cue,
                # the cue's own start time no longer reflects when the
                # kept words were actually spoken — it's when the
                # rolling window opened. Use the inline timestamp of
                # the first KEPT word when available. For untrimmed
                # cues (first cue, manual subs, no overlap) keep the
                # cue's own start verbatim.
                start = current_start
                if trim_offset > 0:
                    for idx, ts in word_starts:
                        if idx >= trim_offset:
                            start = ts
                            break
                out.append((start, trimmed))
                last_text = text  # full text drives next overlap check
        current_start = None
        current_body_lines = []
        in_cue = False

    for raw in vtt.splitlines():
        # Preserve internal whitespace inside cue bodies — strip only
        # the trailing newline characters.
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            # A truly blank line ends the current cue — but only if
            # we've actually collected body text. YouTube auto-captions
            # sometimes emit a single-space "filler" line as the first
            # line after the cue header; treating that as a terminator
            # drops the entire cue. Skip the blank when the body is
            # still empty.
            if in_cue and current_body_lines:
                flush()
            continue
        if stripped == "WEBVTT":
            continue
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if stripped.startswith("Kind:") or stripped.startswith("Language:"):
            # Optional VTT header fields (YouTube emits these).
            continue
        m = _VTT_CUE_HEAD.match(stripped)
        if m:
            flush()
            hh, mm, ss = (int(g) for g in m.groups())
            current_start = float(hh * 3600 + mm * 60 + ss)
            in_cue = True
            continue
        if stripped.isdigit() and not in_cue:
            # Cue identifier line — only valid outside a cue body.
            continue
        if in_cue:
            current_body_lines.append(stripped)
    flush()
    return out


def _trim_overlap_with_offset(
    *, prev: str, current: str
) -> tuple[str, int]:
    """Like `_trim_overlap`, but also returns the word-index in
    `current` where the kept tail starts (0 if no trimming happened).

    The index is used to look up the correct inline timestamp for
    the trimmed tail in YouTube auto-caption cues. Returns ("", 0)
    when `current` is a pure repeat of `prev`.
    """
    if not prev:
        return current, 0
    prev_words = prev.split()
    cur_words = current.split()
    if not cur_words:
        return "", 0
    # Try the longest possible suffix-of-prev / prefix-of-current
    # overlap first.
    max_overlap = min(len(prev_words), len(cur_words))
    for n in range(max_overlap, 0, -1):
        if prev_words[-n:] == cur_words[:n]:
            tail = cur_words[n:]
            return " ".join(tail), n
    # Fully duplicate case: current is a suffix of prev.
    if cur_words == prev_words[-len(cur_words):]:
        return "", len(cur_words)
    return current, 0


def _trim_overlap(*, prev: str, current: str) -> str:
    """Backwards-compatible thin wrapper around
    `_trim_overlap_with_offset` for callers that only need the text."""
    text, _ = _trim_overlap_with_offset(prev=prev, current=current)
    return text


def vtt_to_plain_text(vtt: str) -> str:
    """Backward-compatible plain-text view of the VTT — derived from
    vtt_to_segments so we don't drift between the two parsers."""
    return "\n".join(text for _start, text in vtt_to_segments(vtt))


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


async def _try_download_subtitle(url: str) -> str | None:
    """Wrap _download_text so that 429 and 5xx errors return None
    instead of raising. The caller treats None as 'no subtitle' and
    the pipeline falls back to Whisper.

    Other HTTP errors (auth, 4xx) are not transient and still bubble
    so we don't silently mask broken cookies."""
    try:
        return await _download_text(url)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429 or 500 <= status < 600:
            log.warning(
                "Subtitle fetch transient error %s for %s — falling "
                "back to Whisper",
                status,
                url,
            )
            return None
        raise


_VTT_LANGUAGE_HEADER_RE = re.compile(r"^Language:\s*([a-z]{2})\b", re.M | re.I)


def _parse_vtt_language(vtt: str) -> str | None:
    """Extract the BCP-47-ish two-letter code from the VTT `Language:`
    header that YouTube emits. Returns None when the header is absent.
    """
    m = _VTT_LANGUAGE_HEADER_RE.search(vtt or "")
    return m.group(1).lower() if m else None


async def fetch_subtitles(
    url: str, cookies_path: Path | None
) -> tuple[str, list[tuple[float, str]], SubtitleSource, str | None] | None:
    """Return (plain_text, segments, source, language) or None.

    `segments` is a list of (start_seconds, text) tuples derived from
    the VTT cues — used for timestamped detail-page rendering.
    `language` is the two-letter code from the VTT `Language:` header
    (e.g. "en", "de"), or None if the header was absent.
    """
    info = await asyncio.to_thread(_extract_info_with_subs, url, cookies_path)
    manual_url = _pick_subtitle_url(info, "subtitles")
    if manual_url:
        vtt = await _try_download_subtitle(manual_url)
        if vtt is not None:
            segments = vtt_to_segments(vtt)
            plain = "\n".join(t for _s, t in segments)
            return plain, segments, "manual_subs", _parse_vtt_language(vtt)
    auto_url = _pick_subtitle_url(info, "automatic_captions")
    if auto_url:
        vtt = await _try_download_subtitle(auto_url)
        if vtt is not None:
            segments = vtt_to_segments(vtt)
            plain = "\n".join(t for _s, t in segments)
            return plain, segments, "auto_subs", _parse_vtt_language(vtt)
    return None


def _run_yt_dlp_download(opts: dict[str, Any], url: str) -> None:
    with YoutubeDL(opts) as ydl:
        ydl.download([url])


def _find_audio_file(audio_dir: Path, video_id: str) -> Path | None:
    for path in audio_dir.iterdir():
        if path.stem == video_id:
            return path
    return None


async def download_audio(
    url: str, video_id: str, audio_dir: Path, cookies_path: Path | None
) -> Path:
    await asyncio.to_thread(audio_dir.mkdir, parents=True, exist_ok=True)
    template = str(audio_dir / f"{video_id}.%(ext)s")
    opts = _base_opts(cookies_path)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": template,
    })
    await asyncio.to_thread(_run_yt_dlp_download, opts, url)
    path = await asyncio.to_thread(_find_audio_file, audio_dir, video_id)
    if path:
        return path
    raise RuntimeError(f"Audio download produced no file for {video_id}")
