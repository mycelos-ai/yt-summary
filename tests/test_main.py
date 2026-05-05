from fastapi.testclient import TestClient

from app.main import create_app


def test_root_returns_200():
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "yt-summary" in response.text.lower()
