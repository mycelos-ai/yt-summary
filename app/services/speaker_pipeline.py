"""Best-effort speaker detection for the pipeline.

Deterministic: matches the video's metadata against known_shows
(via show_match) and links the resulting participants into
source_speakers. No LLM, no transcript parsing, no claims — claim
extraction is the PR-3 piggyback. Like pipeline._store_related_links,
this NEVER raises: speaker detection is a nice-to-have and must never
fail the job.
"""
import logging

from app.models import VideoKind
from app.repos import source_speakers as ss_repo
from app.repos import speakers as sp_repo
from app.services import show_match

log = logging.getLogger(__name__)


async def detect_and_link(db, video) -> list[int]:
    """Detect speakers from metadata and link them as source_speakers.

    Returns the resolved speaker_ids linked for this source ([] when
    nothing matched, the video is ineligible, or anything failed)."""
    if video.kind != VideoKind.YOUTUBE or not video.transcript:
        return []
    linked: list[int] = []
    try:
        detected = await show_match.identify_from_metadata(db, video)
        for order, det in enumerate(detected):
            speaker_id = await sp_repo.resolve_speaker(
                db, user_id=video.user_id, name=det.name, role=det.role,
            )
            await ss_repo.link_speaker(
                db, video.id, speaker_id,
                role=det.role, detection_source="show_rule", sort_order=order,
            )
            linked.append(speaker_id)
    except Exception as e:  # noqa: BLE001 — best-effort, must not break the job
        log.warning(
            "speaker detection failed for %s: %s: %s",
            getattr(video, "id", None), type(e).__name__, e,
        )
        return []
    return linked
