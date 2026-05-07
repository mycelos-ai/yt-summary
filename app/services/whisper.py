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
) -> str:
    """Run Whisper on `audio_path` and return the joined transcript.

    If `progress` is provided, it's called as each segment finishes
    with (segment_end_seconds, total_duration_seconds). Faster-whisper
    yields segments lazily, so this gives near-real-time progress.
    """
    model = _load_model(model_name)
    segments, info = model.transcribe(
        str(audio_path), language=None, vad_filter=True
    )
    total = float(getattr(info, "duration", 0.0) or 0.0)
    parts: list[str] = []
    for seg in segments:
        parts.append(seg.text)
        if progress is not None:
            current = float(getattr(seg, "end", 0.0) or 0.0)
            progress(current, total)
    return "".join(parts).strip()


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
) -> str:
    """Send `audio_path` to a hosted Whisper endpoint and return the
    transcript text.

    base_url: the OpenAI-compatible host root, e.g.
      "https://api.groq.com/openai/v1" or "http://mac-mini.local:8000/v1".
      A trailing slash is fine.
    api_key: optional. Empty string disables the Authorization header,
      which is what local servers usually want.
    model_name: server-side model name, e.g. "whisper-large-v3" for
      Groq, "whisper-1" for OpenAI, "Systran/faster-whisper-large-v3"
      for self-hosted faster-whisper-server.
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
        "response_format": "json",
    }

    # trust_env=False so we don't route through the user's HTTP_PROXY
    # — the whisper base_url they configured is explicit and final.
    async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
        resp = await client.post(url, headers=headers, files=files, data=data)
        resp.raise_for_status()
        body = resp.json()
        return (body.get("text") or "").strip()
