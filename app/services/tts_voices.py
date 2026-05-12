"""Curated catalogue of Piper voices we expose in the UI.

Only voices that ship in multiple quality tiers go in the main list
(per design discussion). The Hugging Face URL layout is hard-coded
here — it's the only place that needs to change if rhasspy ever
moves the repo.
"""
from dataclasses import dataclass
from pathlib import Path

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
