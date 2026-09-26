import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    data_dir: Path
    # ── Worker resource limits ─────────────────────────────
    # Local faster-whisper on a Raspberry Pi pins every core with the
    # library default (cpu_threads=0 = all cores) and a 1-hour video
    # takes about an hour. That starves uvicorn, the TTS worker and
    # the host itself. These caps keep the box reachable.
    #
    # whisper_cpu_threads: threads for local faster-whisper. 0 = let
    #   the library decide (all cores).
    # whisper_max_duration_s: videos longer than this get no LOCAL
    #   Whisper fallback and fail with a clear message. Audio is
    #   transcribed in 10-minute chunks, so RAM stays flat; the cap
    #   only bounds how long the CPU stays busy (default 3 h). Hosted
    #   Whisper (base_url set) is not affected. 0 = no cap.
    # job_max_attempts: a summary job that is interrupted (container
    #   killed / rebooted mid-run) is requeued at startup at most this
    #   many times, then marked failed.
    whisper_cpu_threads: int = 2
    whisper_max_duration_s: int = 10800
    job_max_attempts: int = 3

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def tts_voices_dir(self) -> Path:
        return self.data_dir / "tts-voices"

    @property
    def tts_audio_dir(self) -> Path:
        return self.data_dir / "tts-audio"

    @property
    def cookies_path(self) -> Path:
        return self.data_dir / "cookies.txt"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=Path(os.environ.get("YTS_DATA_DIR", "/data")),
            whisper_cpu_threads=_env_int("YTS_WHISPER_CPU_THREADS", 2),
            whisper_max_duration_s=_env_int("YTS_WHISPER_MAX_DURATION_S", 10800),
            job_max_attempts=_env_int("YTS_JOB_MAX_ATTEMPTS", 3),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.tts_voices_dir.mkdir(parents=True, exist_ok=True)
        self.tts_audio_dir.mkdir(parents=True, exist_ok=True)
