import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
from faster_whisper import WhisperModel

_MODEL_CACHE: dict[str, WhisperModel] = {}

# Type alias for the progress callback. Receives (current_seconds,
# total_seconds) — current is the end timestamp of the latest segment
# Whisper finished, total is the audio duration. Both in seconds.
ProgressFn = Callable[[float, float], None]


def _load_model(name: str) -> WhisperModel:
    if name not in _MODEL_CACHE:
        _MODEL_CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODEL_CACHE[name]


def transcribe(
    audio_path: Path,
    model_name: str = "small",
    *,
    progress: ProgressFn | None = None,
) -> tuple[str, list[tuple[float, str]], str | None]:
    """Run Whisper on `audio_path`.

    Returns:
      (joined_text, segments, language)
      where segments is a list of (start_seconds, text) tuples — same
      shape as the VTT parser's output, so downstream code can treat
      both transcript sources uniformly. `language` is faster-whisper's
      detected BCP-47-ish code from `info.language` (e.g. "en", "de"),
      or None if the backend didn't surface one.

    If `progress` is provided, it's called as each segment finishes
    with (segment_end_seconds, total_duration_seconds). Faster-whisper
    yields segments lazily, so this gives near-real-time progress.
    """
    model = _load_model(model_name)
    segments, info = model.transcribe(
        str(audio_path), language=None, vad_filter=True
    )
    total = float(getattr(info, "duration", 0.0) or 0.0)
    raw_lang = getattr(info, "language", None)
    language: str | None = None
    if isinstance(raw_lang, str) and raw_lang.strip():
        language = raw_lang.strip().lower()
    parts: list[str] = []
    timed: list[tuple[float, str]] = []
    for seg in segments:
        parts.append(seg.text)
        start = float(getattr(seg, "start", 0.0) or 0.0)
        text = (seg.text or "").strip()
        if text:
            timed.append((start, text))
        if progress is not None:
            current = float(getattr(seg, "end", 0.0) or 0.0)
            progress(current, total)
    return "".join(parts).strip(), timed, language


# Hosted Whisper services (faster-whisper-server, Groq, OpenAI) all
# speak the same /v1/audio/transcriptions multipart contract. We POST
# the audio file plus the model name and read back {"text": "..."}.
async def transcribe_via_api(
    audio_path: Path,
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    timeout_s: float = 300.0,
) -> tuple[str, list[tuple[float, str]], str | None]:
    """Send `audio_path` to a hosted Whisper endpoint.

    Returns (joined_text, segments, language). Asks the OpenAI-
    compatible endpoint for `response_format=verbose_json`, which
    includes per-segment start times and a top-level `language` field.
    If the backend ignores that and returns only `{"text": "..."}`,
    segments comes back empty and language is None — the detail-page
    render falls back to the plain transcript.

    base_url: e.g. "https://api.groq.com/openai/v1" or
      "http://mac-mini.local:8000/v1". A trailing slash is fine.
    api_key: optional. Empty disables the Authorization header,
      which is what most local servers want.
    model_name: server-side name, e.g. "whisper-large-v3" for Groq,
      "whisper-1" for OpenAI, "Systran/faster-whisper-large-v3" for
      self-hosted faster-whisper-server.
    """
    url = f"{base_url.rstrip('/')}/audio/transcriptions"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Reading the whole file into memory is fine — yt-dlp audio is
    # ≤100MB even for 1h videos and the request body has to be one
    # blob anyway. asyncio.to_thread keeps the event loop responsive.
    audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
    files = {
        "file": (audio_path.name, audio_bytes, "audio/mpeg"),
    }
    data = {
        "model": model_name,
        # verbose_json gives back a `segments` array with start/end
        # times. OpenAI + faster-whisper-server both honour it; older
        # servers fall back to plain `{text}` which we handle below.
        "response_format": "verbose_json",
    }

    async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
        resp = await client.post(url, headers=headers, files=files, data=data)
        resp.raise_for_status()
        body = resp.json()

    text = (body.get("text") or "").strip()
    timed: list[tuple[float, str]] = []
    for seg in body.get("segments") or []:
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue
        timed.append((float(seg.get("start", 0.0) or 0.0), seg_text))
    raw_lang = body.get("language")
    language: str | None = None
    if isinstance(raw_lang, str) and raw_lang.strip():
        language = raw_lang.strip().lower()
    return text, timed, language
