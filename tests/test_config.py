
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


def test_config_has_tts_voices_and_audio_dirs(tmp_path):
    from app.config import Config
    cfg = Config(data_dir=tmp_path)
    assert cfg.tts_voices_dir == tmp_path / "tts-voices"
    assert cfg.tts_audio_dir == tmp_path / "tts-audio"
    cfg.ensure_dirs()
    assert cfg.tts_voices_dir.exists()
    assert cfg.tts_audio_dir.exists()


def test_config_worker_limits_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YTS_WHISPER_CPU_THREADS", raising=False)
    monkeypatch.delenv("YTS_WHISPER_MAX_DURATION_S", raising=False)
    monkeypatch.delenv("YTS_JOB_MAX_ATTEMPTS", raising=False)
    cfg = Config.from_env()
    assert cfg.whisper_cpu_threads == 2
    assert cfg.whisper_max_duration_s == 10800
    assert cfg.job_max_attempts == 3


def test_config_worker_limits_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YTS_WHISPER_CPU_THREADS", "4")
    monkeypatch.setenv("YTS_WHISPER_MAX_DURATION_S", "0")
    monkeypatch.setenv("YTS_JOB_MAX_ATTEMPTS", "5")
    cfg = Config.from_env()
    assert cfg.whisper_cpu_threads == 4
    assert cfg.whisper_max_duration_s == 0
    assert cfg.job_max_attempts == 5
