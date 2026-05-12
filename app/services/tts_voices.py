"""Curated catalogue of Piper voices we expose in the UI.

Only voices that ship in multiple quality tiers go in the main list
(per design discussion). The Hugging Face URL layout is hard-coded
here — it's the only place that needs to change if rhasspy ever
moves the repo.
"""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


@dataclass(frozen=True)
class Voice:
    language: str          # 'de' | 'en_US' | 'en_GB' | 'fr' | 'es'
    id: str                # 'thorsten', 'lessac', …
    qualities: tuple[str, ...]
    display_name: str


VOICES: tuple[Voice, ...] = (
    Voice("de",    "thorsten",                ("low", "medium", "high"),
          "Thorsten (m, neutral)"),
    Voice("de",    "thorsten_emotional",      ("medium",),
          "Thorsten (m, emotional)"),
    Voice("de",    "kerstin",                 ("low",),
          "Kerstin (f)"),
    Voice("en_US", "lessac",                  ("low", "medium", "high"),
          "Lessac (f)"),
    Voice("en_US", "amy",                     ("low", "medium"),
          "Amy (f)"),
    Voice("en_US", "ryan",                    ("low", "medium", "high"),
          "Ryan (m)"),
    Voice("en_GB", "alba",                    ("medium",),
          "Alba (f)"),
    Voice("en_GB", "southern_english_female", ("low", "medium"),
          "Southern English (f)"),
    Voice("fr",    "siwis",                   ("low", "medium"),
          "Siwis (f)"),
    Voice("es",    "sharvard",                ("medium",),
          "Sharvard (m)"),
)


# language code → Hugging Face directory naming. Piper organises
# voices under `<lang>/<lang>_<REGION>/...` where REGION is the
# uppercase ISO country code. We map our short codes here so the
# rest of the code stays clean.
_HF_LANG_DIR = {
    "de":    ("de", "de_DE"),
    "en_US": ("en", "en_US"),
    "en_GB": ("en", "en_GB"),
    "fr":    ("fr", "fr_FR"),
    "es":    ("es", "es_ES"),
}


def voices_for_language(language: str) -> tuple[Voice, ...]:
    return tuple(v for v in VOICES if v.language == language)


def qualities_for_voice(language: str, voice_id: str) -> tuple[str, ...]:
    for v in VOICES:
        if v.language == language and v.id == voice_id:
            return v.qualities
    return ()


def _voice_filename(language: str, voice_id: str, quality: str) -> str:
    _, region = _HF_LANG_DIR[language]
    return f"{region}-{voice_id}-{quality}.onnx"


def voice_file_path(
    voices_dir: Path, language: str, voice_id: str, quality: str
) -> Path:
    return voices_dir / _voice_filename(language, voice_id, quality)


def voice_download_urls(
    language: str, voice_id: str, quality: str
) -> tuple[str, str]:
    """(.onnx URL, .onnx.json URL)"""
    lang_root, region = _HF_LANG_DIR[language]
    onnx_name = _voice_filename(language, voice_id, quality)
    onnx_url = (
        f"{HF_BASE}/{lang_root}/{region}/{voice_id}/{quality}/{onnx_name}"
    )
    return onnx_url, onnx_url + ".json"


ProgressCb = Callable[[int, int], None]
"""(bytes_done, bytes_total). bytes_total=0 means the server didn't
send a Content-Length header — treat as indeterminate."""


async def _download_to_path(
    url: str, target: Path, progress: ProgressCb | None
) -> None:
    """Stream `url` to `target` via `target.partial` then atomic rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    try:
        # Generous per-read timeout (300s) covers slow connections without
        # letting a stalled connection hang forever. Total transfer time
        # is unbounded — a 130 MB high-quality voice on a 1 Mbit/s link
        # legitimately takes minutes.
        timeout = httpx.Timeout(300.0, connect=10.0)
        async with (
            httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, trust_env=False,
            ) as c,
            c.stream("GET", url) as r,
        ):
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with partial.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


async def download_voice(
    voices_dir: Path,
    language: str,
    voice_id: str,
    quality: str,
    *,
    progress: ProgressCb | None = None,
) -> Path:
    """Download both files for the voice if missing. Returns the
    path to the .onnx model.

    The progress callback fires only for the .onnx download; the small
    .onnx.json file's progress is suppressed to avoid confusing UIs
    that are tracking the main model byte count.

    Not safe for concurrent calls with the same (language, voice_id,
    quality) — both calls would write to the same .partial file. The
    TtsWorker processes one job at a time, so this isn't enforced in
    code.
    """
    onnx_path = voice_file_path(voices_dir, language, voice_id, quality)
    json_path = onnx_path.parent / (onnx_path.name + ".json")
    if onnx_path.exists() and json_path.exists():
        return onnx_path
    onnx_url, json_url = voice_download_urls(language, voice_id, quality)
    if not onnx_path.exists():
        await _download_to_path(onnx_url, onnx_path, progress)
    if not json_path.exists():
        await _download_to_path(json_url, json_path, progress=None)
    return onnx_path
