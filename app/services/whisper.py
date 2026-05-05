from pathlib import Path

from faster_whisper import WhisperModel

_MODEL_CACHE: dict[str, WhisperModel] = {}


def _load_model(name: str) -> WhisperModel:
    if name not in _MODEL_CACHE:
        _MODEL_CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODEL_CACHE[name]


def transcribe(audio_path: Path, model_name: str = "small") -> str:
    model = _load_model(model_name)
    segments, _info = model.transcribe(str(audio_path), language=None, vad_filter=True)
    return "".join(seg.text for seg in segments).strip()
