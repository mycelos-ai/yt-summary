"""Test that channel_id is captured from yt-dlp info."""

from app.services.youtube import VideoMetadata


def test_video_metadata_captures_channel_id():
    """VideoMetadata should capture channel_id from yt-dlp info."""
    meta = VideoMetadata(
        id="dQw4w9WgXcQ",
        url="https://youtube.com/watch?v=dQw4w9WgXcQ",
        title="Test Video",
        description="A test video",
        duration_seconds=120,
        thumbnail_url="https://example.com/thumb.jpg",
        channel_id="UCxyz",
    )
    assert meta.channel_id == "UCxyz"


def test_video_metadata_channel_id_optional():
    """VideoMetadata should allow channel_id to be None."""
    meta = VideoMetadata(
        id="dQw4w9WgXcQ",
        url="https://youtube.com/watch?v=dQw4w9WgXcQ",
        title="Test Video",
        description="A test video",
        duration_seconds=120,
        thumbnail_url="https://example.com/thumb.jpg",
    )
    assert meta.channel_id is None
