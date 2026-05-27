"""Feedback endpoints.

POST /feedback         create a feedback row, schedule a consolidate
DELETE /feedback/<id>  remove a feedback row (owner only)

All requests are scoped to the active Profile via the existing cookie/
get_current_user dependency. The body uses Pydantic validation to
enforce offset and text-length constraints.
"""
from __future__ import annotations

import asyncio

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.main import get_current_user_id, get_db
from app.models import FeedbackSource, Sentiment
from app.repos import digests as digests_repo
from app.repos import feedback as feedback_repo
from app.repos import videos as videos_repo
from app.services import interest_profile as profile_service

router = APIRouter()

# Strong references to in-flight consolidate tasks. asyncio.create_task
# only weak-references the resulting Task; without a strong ref the loop
# can GC it before it runs, and consolidate silently never happens.
# Tasks self-discard from this set when done (add_done_callback below).
_PENDING_CONSOLIDATES: set[asyncio.Task] = set()


class FeedbackIn(BaseModel):
    # Exactly one of video_id / digest_id is set. The route validates
    # ownership of whichever is provided.
    video_id: str | None = None
    digest_id: int | None = None
    source: FeedbackSource
    selected_text: str = Field(..., min_length=1, max_length=1000)
    text_offset_start: int = Field(..., ge=0)
    text_offset_end: int = Field(..., ge=1)
    sentiment: Sentiment
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check(self) -> FeedbackIn:
        if self.text_offset_end <= self.text_offset_start:
            raise ValueError("text_offset_end must be > text_offset_start")
        if (self.video_id is None) == (self.digest_id is None):
            raise ValueError(
                "exactly one of video_id / digest_id must be set"
            )
        return self


@router.post("/feedback")
async def create_feedback(
    payload: FeedbackIn,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> dict:
    # Confirm the anchor (video OR digest) belongs to this Profile.
    if payload.video_id is not None:
        video = await videos_repo.get(db, payload.video_id)
        if video is None or video.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your video")
    else:
        assert payload.digest_id is not None  # XOR guaranteed by validator
        d = await digests_repo.get(db, payload.digest_id)
        if d is None or d.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your digest")

    fb = await feedback_repo.create(
        db,
        user_id=user_id,
        video_id=payload.video_id,
        digest_id=payload.digest_id,
        source=payload.source,
        selected_text=payload.selected_text,
        text_offset_start=payload.text_offset_start,
        text_offset_end=payload.text_offset_end,
        sentiment=payload.sentiment,
        comment=payload.comment,
    )

    # Schedule consolidate in the background — don't block the request.
    # Hold a strong reference to the task until it completes; otherwise
    # the event loop is permitted to GC it before it runs.
    task = asyncio.create_task(
        profile_service.consolidate(db, user_id=user_id),
    )
    _PENDING_CONSOLIDATES.add(task)
    task.add_done_callback(_PENDING_CONSOLIDATES.discard)

    return {
        "id": fb.id,
        "sentiment": fb.sentiment.value,
        "created_at": fb.created_at.isoformat(),
    }


@router.delete("/feedback/{feedback_id}")
async def delete_feedback(
    feedback_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> dict:
    deleted = await feedback_repo.delete(
        db, feedback_id=feedback_id, user_id=user_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}
