import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path

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
    def cookies_path(self) -> Path:
        return self.data_dir / "cookies.txt"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(data_dir=Path(os.environ.get("YTS_DATA_DIR", "/data")))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
