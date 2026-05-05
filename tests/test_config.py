
from app.config import Config


def test_config_default_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.data_dir == tmp_path
    assert cfg.db_path == tmp_path / "app.db"
    assert cfg.thumbnails_dir == tmp_path / "thumbnails"
    assert cfg.audio_dir == tmp_path / "audio"
    assert cfg.cookies_path == tmp_path / "cookies.txt"


def test_config_creates_subdirs(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    cfg.ensure_dirs()
    assert (tmp_path / "thumbnails").is_dir()
    assert (tmp_path / "audio").is_dir()
