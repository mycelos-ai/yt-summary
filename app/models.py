from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


class TranscriptSource(StrEnum):
    MANUAL_SUBS = "manual_subs"
    AUTO_SUBS = "auto_subs"
    WHISPER = "whisper"
    WEB = "web"
    EMAIL = "email"


class VideoKind(StrEnum):
    YOUTUBE = "youtube"
    WEB = "web"
    EMAIL = "email"


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
    # JSON-encoded list of {text, rank, reason}. NULL = not yet extracted
    # (pre-feature backlog). "[]" = LLM said "nothing noteworthy".
    highlights_json: str | None = None


@dataclass
class Job:
    id: int
    video_id: str
    state: JobState
    step: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    llm_model_id: int | None = None
    additional_prompt: str | None = None


@dataclass
class TtsJob:
    id: int
    video_id: str
    source: str  # 'summary' | 'transcript'
    target_language: str
    voice: str
    quality: str  # 'low' | 'medium' | 'high'
    status: str  # 'queued' | 'translating' | 'rendering' | 'done' | 'failed'
    step: str | None
    translated_text: str | None
    audio_path: str | None
    duration_seconds: float | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


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
    interest_profile_md: str | None = None
    interest_profile_version: int = 0
    digest_enabled: bool = False
    digest_hour_local: int = 7
    # Part B: capability token for the personal podcast feed. None →
    # feed disabled. Plaintext (the settings page re-displays the URL).
    podcast_token: str | None = None


@dataclass
class MailSender:
    """A distinct From-address seen in a profile's mailbox.

    Discovered by scanning recent messages. `subscribed` drives whether
    the sync ingests this sender's mail — newsletters are strictly
    opt-in, so only subscribed senders are crawled.
    """
    user_id: int
    sender_addr: str
    sender_name: str
    subscribed: bool
    last_seen_at: str | None
    last_subject: str | None


@dataclass
class LlmModel:
    id: int
    label: str
    provider_id: str
    model: str
    api_key: str
    base_url: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class FeedbackSource(StrEnum):
    SUMMARY = "summary"
    TRANSCRIPT = "transcript"
    DIGEST = "digest"  # Feedback on a digest's source-item (hook/reason)
    DIGEST_TLDR = "digest_tldr"  # Feedback on a digest's TL;DR block


class Sentiment(StrEnum):
    INTERESTING = "interesting"
    NOT_INTERESTING = "not_interesting"


class DigestStatus(StrEnum):
    PENDING = "pending"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"


@dataclass
class Feedback:
    id: int
    user_id: int
    # Exactly one of video_id / digest_id is set (CHECK constraint at
    # the DB level). video_id anchors per-video feedback (summary,
    # transcript, digest source item); digest_id anchors TL;DR feedback
    # which doesn't have a single owning video.
    video_id: str | None
    digest_id: int | None
    source: FeedbackSource
    selected_text: str
    text_offset_start: int
    text_offset_end: int
    sentiment: Sentiment
    comment: str | None
    created_at: datetime


@dataclass
class Digest:
    id: int
    user_id: int
    period_start: datetime
    period_end: datetime
    tldr: str | None
    # JSON-encoded list of {video_id, rank, hook, reason}. NULL while
    # the digest is pending/rendering; "[]" when status='ready' but
    # the pool was empty.
    top_items_json: str | None
    item_count: int
    status: DigestStatus
    error: str | None
    # JSON-encoded list of hand-picked video ids (manual digests).
    # None = automatic digest: pool is everything in the window.
    selected_video_ids_json: str | None
    created_at: datetime


class SynthesisStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass
class Synthesis:
    """One "ask my library" answer — a question answered across the
    Profile's stored summaries, with citations. States pending → ready |
    failed. A knowledge artifact, not throwaway chat: persisted and
    listed like digests."""
    id: int
    user_id: int
    query: str
    result_md: str | None
    # JSON-encoded ordered list of the video ids used as sources.
    source_ids_json: str
    status: SynthesisStatus
    error: str | None
    created_at: datetime


@dataclass
class Highlight:
    """One LLM-extracted noteworthy point from a summary.

    Not a DB row — serialised inside `videos.highlights_json` as a list.
    """
    text: str
    rank: int  # 1..5 (1 = most noteworthy)
    reason: str
