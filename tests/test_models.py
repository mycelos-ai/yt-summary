from datetime import datetime

from app.models import ChatMessage, Job, JobState, TranscriptSource, Video


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
