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
    # JSON-serialised list of {"start": float, "text": str} segments.
    # YouTube videos transcribed via Whisper or VTT have segments;
    # web articles + older Whisper rows have None.
    transcript_segments: str | None = None
    # Owning profile (multi-profile feature). All existing rows on
    # pre-V5 installs default to user_id=1 via the schema migration.
    user_id: int = 1
    # Bare 11-char YouTube id (separate from `id` so we can dedupe
    # transcripts across profiles). NULL for web articles.
    youtube_id: str | None = None
    # Language metadata stamped during processing (V6 migration).
    # `source_language` is what the video is in (best signal across
    # Whisper / VTT / LLM-detect fallback). `transcript_language`
    # matches `source_language` in 100% of known cases — kept
    # separate so later features (e.g. translated transcripts) can
    # diverge them. `summary_language` is what the summary itself
    # was generated in (driven by the `summary_language` setting,
    # which can be "auto" or an explicit two-letter code).
    source_language: str | None = None
    summary_language: str | None = None
    transcript_language: str | None = None


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
    # Profile-specific fields. avatar_emoji is the header / picker glyph.
    # custom_summary_prompt overrides the standard summarizer system
    # prompt for this profile (None → use the standard prompt).
    avatar_emoji: str = "👤"
    # Image-based avatar — name of a file (without extension) under
    # app/static/avatars/. Empty string → use avatar_emoji instead.
    # The picker on the profile form lets the user choose from the
    # curated library; the emoji input is a fallback / quick option.
    avatar_image: str = ""
    custom_summary_prompt: str | None = None
