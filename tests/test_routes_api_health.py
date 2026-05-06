from fastapi.testclient import TestClient

from app.main import create_app


def test_api_health_open(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "version" in body
