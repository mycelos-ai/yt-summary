from collections.abc import Callable
from pathlib import Path

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
