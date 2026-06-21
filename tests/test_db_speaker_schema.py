from app.models import VideoKind


def test_videokind_has_text():
    assert VideoKind.TEXT == "text"
    assert VideoKind("text") is VideoKind.TEXT
