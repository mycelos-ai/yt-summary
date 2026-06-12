from datetime import datetime

from app.models import (
    ChatMessage,
    Digest,
    DigestStatus,
    Feedback,
    FeedbackSource,
    Highlight,
    Job,
    JobState,
    Sentiment,
    TranscriptSource,
    Video,
)


def test_video_dataclass():
    v = Video(
        id="abc123",
        url="https://youtu.be/abc123",
        title="Hello",
        description="desc",
        thumbnail_path=None,
        duration_seconds=600,
        transcript=None,
        transcript_source=None,
        summary=None,
        summary_model=None,
        created_at=datetime(2026, 5, 5),
        updated_at=datetime(2026, 5, 5),
    )
    assert v.id == "abc123"
    assert v.summary is None


def test_transcript_source_enum_values():
    assert TranscriptSource.MANUAL_SUBS.value == "manual_subs"
    assert TranscriptSource.AUTO_SUBS.value == "auto_subs"
    assert TranscriptSource.WHISPER.value == "whisper"


def test_job_state_enum_values():
    assert {s.value for s in JobState} == {"pending", "running", "done", "failed"}


def test_chat_message_dataclass():
    msg = ChatMessage(
        id=1,
        video_id="abc",
        role="user",
        content="hi",
        created_at=datetime(2026, 5, 5),
    )
    assert msg.role == "user"


def test_job_dataclass():
    j = Job(
        id=1,
        video_id="abc",
        state=JobState.PENDING,
        step="",
        error_message=None,
        created_at=datetime(2026, 5, 5),
        updated_at=datetime(2026, 5, 5),
    )
    assert j.state is JobState.PENDING


def test_playlist_dataclass():
    from app.models import Playlist
    p = Playlist(
        id="PLh9GXHYeT6w",
        user_id=1,
        url="https://www.youtube.com/playlist?list=PLh9GXHYeT6w",
        title="My playlist",
        description="",
        thumbnail_path=None,
        last_refreshed_at=None,
        created_at=datetime(2026, 5, 6),
    )
    assert p.id == "PLh9GXHYeT6w"
    assert p.user_id == 1
    assert p.last_refreshed_at is None


def test_user_dataclass():
    from app.models import User
    u = User(
        id=1,
        name="admin",
        api_key_hash=None,
        api_key_prefix=None,
        api_key_created_at=None,
        created_at=datetime(2026, 5, 6),
    )
    assert u.id == 1
    assert u.name == "admin"
    assert u.api_key_hash is None


def test_feedback_dataclass_video_anchor():
    fb = Feedback(
        id=1, user_id=2, video_id="v1", digest_id=None,
        source=FeedbackSource.SUMMARY,
        selected_text="some text",
        text_offset_start=0, text_offset_end=9,
        sentiment=Sentiment.INTERESTING,
        comment=None,
        created_at=datetime(2026, 5, 26, 12, 0),
    )
    assert fb.video_id == "v1"
    assert fb.digest_id is None
    assert fb.sentiment == "interesting"


def test_feedback_dataclass_digest_tldr_anchor():
    """A digest-TLDR feedback row has digest_id set and video_id None.
    The DB CHECK enforces XOR; the dataclass just stores both fields."""
    fb = Feedback(
        id=2, user_id=2, video_id=None, digest_id=5,
        source=FeedbackSource.DIGEST_TLDR,
        selected_text="tldr line",
        text_offset_start=0, text_offset_end=9,
        sentiment=Sentiment.INTERESTING,
        comment=None,
        created_at=datetime(2026, 5, 26, 12, 0),
    )
    assert fb.video_id is None
    assert fb.digest_id == 5
    assert fb.source == "digest_tldr"


def test_digest_dataclass():
    d = Digest(
        id=1, user_id=2,
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 5, 26),
        tldr="t", top_items_json="[]",
        item_count=0,
        status=DigestStatus.READY,
        error=None,
        created_at=datetime(2026, 5, 26, 7, 0),
        selected_video_ids_json=None,
    )
    assert d.status == "ready"


def test_highlight_dataclass():
    h = Highlight(text="key insight", rank=1, reason="matters")
    assert h.rank == 1
