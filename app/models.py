from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


class TranscriptSource(StrEnum):
    MANUAL_SUBS = "manual_subs"
    AUTO_SUBS = "auto_subs"
    WHISPER = "whisper"
    WEB = "web"


class VideoKind(StrEnum):
    YOUTUBE = "youtube"
    WEB = "web"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


ChatRole = Literal["user", "assistant"]


@dataclass
class Video:
    id: str
    url: str
    title: str
    description: str
    thumbnail_path: str | None
    duration_seconds: int | None
    transcript: str | None
    transcript_source: TranscriptSource | None
    summary: str | None
    summary_model: str | None
    created_at: datetime
    updated_at: datetime
    kind: VideoKind = VideoKind.YOUTUBE


@dataclass
class Job:
    id: int
    video_id: str
    state: JobState
    step: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class ChatMessage:
    id: int
    video_id: str
    role: ChatRole
    content: str
    created_at: datetime


@dataclass
class Playlist:
    id: str
    user_id: int
    url: str
    title: str
    description: str
    thumbnail_path: str | None
    last_refreshed_at: datetime | None
    created_at: datetime


@dataclass
class User:
    id: int
    name: str
    api_key_hash: str | None
    api_key_prefix: str | None
    api_key_created_at: datetime | None
    created_at: datetime
