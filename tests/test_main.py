from fastapi.testclient import TestClient

from app.main import create_app


def test_root_returns_200(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        response = client.get("/")
    assert response.status_code == 200


def test_app_creates_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        client.get("/")
    assert (tmp_path / "thumbnails").is_dir()
    assert (tmp_path / "audio").is_dir()
    assert (tmp_path / "app.db").is_file()
