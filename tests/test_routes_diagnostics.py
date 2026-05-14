import logging

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.heartbeat import HeartbeatRegistry


def test_app_state_exposes_heartbeats_and_log_buffer(tmp_path, monkeypatch):
    """The diagnostics page reads these off app.state; lifespan must set them."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        # TestClient enters lifespan on __enter__.
        assert hasattr(app.state, "heartbeats")
        assert isinstance(app.state.heartbeats, HeartbeatRegistry)
        assert hasattr(app.state, "log_buffer")
        # And the log buffer is wired to the root logger.
        root = logging.getLogger()
        assert app.state.log_buffer in root.handlers
    # After shutdown, the handler must be removed so the next
    # create_app() doesn't accumulate handlers.
    assert app.state.log_buffer not in logging.getLogger().handlers


def test_log_buffer_captures_emitted_lines(tmp_path, monkeypatch):
    """Smoke: any logger.info() should land in the ring buffer."""
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app):
        logging.getLogger("yt_summary.test").info("hello-from-test")
        lines = app.state.log_buffer.snapshot()
        assert any("hello-from-test" in line for line in lines)
