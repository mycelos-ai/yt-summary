import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite

from app.config import Config
from app.models import TranscriptSource, Video, VideoKind
from app.repos import embeddings as embeddings_repo
from app.repos import llm_models as llm_models_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import tags as tags_repo
from app.repos import users as users_repo
from app.repos import videos as videos_repo
from app.services import related_links
from app.services.embeddings import embed_text
from app.services.language_detect import detect_language
from app.services.reader import fetch_article
from app.services.summarizer import (
    _completion,
    _verify_summary_timestamps,
    summarize_with_highlights,
)
from app.services.transcript import obtain_transcript
from app.services.transcript_format import group_segments
from app.services.youtube import fetch_metadata

log = logging.getLogger(__name__)


def _resolve_cookies(config: Config) -> Path | None:
    p = config.cookies_path
    return p if p.exists() else None


async def process_video(
    db: aiosqlite.Connection,
    config: Config,
    video_id: str,
    set_step: Callable[[str], Awaitable[None]],
    *,
    llm_model_id: int | None = None,
    additional_prompt: str | None = None,
) -> None:
    video = await videos_repo.get(db, video_id)
    if video is None:
        raise RuntimeError(f"Video {video_id} not found")

    settings = await settings_repo.get_all(db)
    whisper_model = settings.get("whisper_model", "small")

    # Resolve which LLM to use. Background work (auto-import, initial
    # submit) passes llm_model_id=None and we use the default row.
    # The Re-summarize panel may override either or both fields.
    model_row = (
        await llm_models_repo.get(db, llm_model_id)
        if llm_model_id is not None
        else await llm_models_repo.get_default(db)
    )
    model = model_row.model if model_row else None
    api_key = model_row.api_key if model_row else ""
    base_url = (model_row.base_url or None) if model_row else None

    cookies = await asyncio.to_thread(_resolve_cookies, config)

    # Decide whether to fetch the transcript fresh, or reuse what's
    # already stored:
    #
    # - WEB articles: only fetch if no transcript stored.
    # - YouTube: fetch if either no transcript at all, OR no segments
    #   yet. The latter is the self-healing path for videos that were
    #   transcribed before the timestamps feature shipped — they
    #   already have plain text but no JSON segments, so we re-fetch
    #   to populate them.
    needs_fetch = (
        not video.transcript
        or (
            video.kind == VideoKind.YOUTUBE
            and not video.transcript_segments
        )
    )
    # `source_language` is the best signal we have for what language
    # this video is in. We accumulate it across the transcript path
    # (Whisper / VTT) and fall back to LLM-detect after summarization
    # if nothing surfaced earlier.
    source_lang: str | None = video.source_language
    if needs_fetch:
        # Cross-profile transcript reuse: if another profile in this
        # household already transcribed the same YouTube video (or
        # fetched the same article), copy the transcript over instead
        # of running Whisper / yt-dlp again. Saves both wall-clock
        # time (Whisper on Pi5 is slow) and API cost (Groq Whisper
        # would re-bill). Each profile still summarises with its own
        # custom prompt — only the underlying transcript is shared.
        donor: Video | None = None
        if video.kind == VideoKind.YOUTUBE:
            yt_id = (
                getattr(video, "youtube_id", None)
                or video.id.split(":", 1)[-1]
            )
            if yt_id:
                donor = await videos_repo.find_other_with_transcript(
                    db, youtube_id=yt_id, exclude_user_id=video.user_id,
                )
        else:
            donor = await videos_repo.find_other_with_transcript_by_url(
                db, url=video.url, exclude_user_id=video.user_id,
            )

        if donor and donor.transcript:
            await set_step("transcript reused from another profile")
            donor_lang = donor.source_language or donor.transcript_language
            await videos_repo.set_transcript(
                db,
                video_id,
                donor.transcript,
                donor.transcript_source or TranscriptSource.AUTO_SUBS,
                segments_json=donor.transcript_segments,
                language=donor_lang,
            )
            text = donor.transcript
            if donor_lang:
                source_lang = donor_lang
        elif video.kind == VideoKind.WEB:
            await set_step("fetching article")
            article = await fetch_article(video.url)
            # The reader doesn't expose a language signal, so we leave
            # the columns NULL here and rely on the LLM-detect
            # fallback once the summary is in.
            await videos_repo.set_transcript(
                db, video_id, article.body, TranscriptSource.WEB
            )
            text = article.body
        else:
            await set_step("fetching transcript")
            text, segments, source, transcript_lang = await obtain_transcript(
                url=video.url,
                video_id=video_id,
                audio_dir=config.audio_dir,
                cookies_path=cookies,
                whisper_model=whisper_model,
                progress_cb=set_step,
                whisper_base_url=settings.get("whisper_base_url", ""),
                whisper_api_key=settings.get("whisper_api_key", ""),
            )
            # Group raw cues into 8-second-gap paragraphs and JSON-
            # serialise — the detail page renders blocks with leading
            # [MM:SS] timestamps that link back into the YouTube video.
            segments_json: str | None = None
            if segments:
                grouped = group_segments(segments, gap_s=8.0)
                if grouped:
                    segments_json = json.dumps(grouped)
            await videos_repo.set_transcript(
                db, video_id, text, source, segments_json=segments_json,
                language=transcript_lang,
            )
            if transcript_lang:
                source_lang = transcript_lang
    else:
        text = video.transcript

    # Backfill tags only for YouTube videos (yt-dlp surfaces them).
    # Web pages have no equivalent metadata field worth chasing.
    if video.kind == VideoKind.YOUTUBE:
        existing_tags = await tags_repo.tags_for_video(db, video_id)
        if not existing_tags:
            try:
                meta = await fetch_metadata(video.url, cookies_path=cookies)
                if meta.tags:
                    await tags_repo.set_tags_for_video(
                        db, video_id, list(meta.tags)
                    )
            except Exception:
                # Tag backfill is a nice-to-have; don't fail the whole job.
                pass

    if not model:
        await set_step("transcript only (no LLM model configured)")
        return

    await set_step("summarizing")

    async def _persist_partial(partial: str) -> None:
        # Map-reduce: surface progress to the UI by writing the working
        # summary back to the videos row. The detail page polls this
        # while the job is running.
        await videos_repo.set_summary(db, video_id, partial, model)

    # Playlists this video lives in are topical hints — the user files
    # videos thematically (e.g. "AI", "Long-form interviews"), so we
    # surface those names to the summarizer for better focus.
    playlist_links = await playlists_repo.playlists_for_videos(db, [video_id])
    playlist_context = [title for _id, title in playlist_links.get(video_id, [])]

    # Surface segments to the summarizer ONLY for YouTube videos —
    # web articles have no notion of time, so timestamp links would be
    # nonsense there. Re-load the video so we pick up segments that
    # were just persisted by the fetch branch above.
    if needs_fetch:
        video = await videos_repo.get(db, video_id)
        if video is None:
            raise RuntimeError(f"Video {video_id} disappeared mid-pipeline")
    segments = _segments_for_summarizer(video)

    # We're here only if `text` was set above — either from a fresh
    # fetch (always str) or from `video.transcript` (which we already
    # gated on via `needs_fetch`). Reassure the type checker.
    assert text is not None, "text must be set before summarization"

    # The summarizer's system prompt is per-profile. Each user's
    # custom_summary_prompt is seeded from the standard prompt when
    # the profile is created (or via the migration for the seeded
    # admin user), and is fully editable from the profile page.
    # There's no longer a hardcoded fallback inside the summarizer
    # itself — what's stored on the user IS the prompt.
    profile = await users_repo.get_by_id(db, video.user_id)
    custom_prompt = profile.custom_summary_prompt if profile else None
    profile_md, _profile_version = await users_repo.get_interest_profile(
        db, user_id=video.user_id,
    )

    summary_language_setting = (settings.get("summary_language") or "").strip()
    # Email-kind items get the newsletter-tuned prompt (triage + drop
    # ad/tracking/footer cruft); everything else uses the standard path.
    content_kind = "email" if video.kind == VideoKind.EMAIL else "youtube"
    summary, highlights = await summarize_with_highlights(
        transcript=text,
        model=model,
        api_key=api_key or "",
        base_url=base_url,
        title=video.title,
        description=video.description,
        language=summary_language_setting or None,
        custom_system_prompt=custom_prompt,
        interest_profile_md=profile_md,
        playlist_context=playlist_context or None,
        transcript_segments=segments,
        additional_prompt=additional_prompt,
        content_kind=content_kind,
        progress=set_step,
        on_partial=_persist_partial,
    )

    if highlights is not None:
        await videos_repo.set_highlights(
            db, video_id, json.dumps(highlights, ensure_ascii=False),
        )
    # When highlights is None (LLM didn't follow the JSON envelope, or
    # transcript was below the highlights threshold), we leave
    # highlights_json untouched (NULL). The Digest service filters
    # NULL items out of its pool.

    # Stock thumbnail for email/web items lacking one. Cosmetic — every
    # failure is swallowed inside the stock_images helpers, so this can
    # never break the pipeline. Skipped entirely when no Pexels key is
    # configured for the owning profile.
    if video.kind in (VideoKind.EMAIL, VideoKind.WEB) and not video.thumbnail_path:
        from app.services import stock_images
        pexels_key = await settings_repo.get_for_user(
            db, video.user_id, "pexels_api_key",
        ) or ""
        if pexels_key:
            image_query = await stock_images.generate_image_query(
                summary=summary or "", model_row=model_row,
            )
            if image_query:
                await videos_repo.set_image_query(db, video_id, image_query)
                refreshed = await videos_repo.get(db, video_id)
                if refreshed is not None:
                    await stock_images.ensure_stock_thumbnail(
                        db, refreshed, config=config, api_key=pexels_key,
                        force=False,
                    )

    # Resolve the language metadata to stamp on the final write:
    #   * If source_language is still NULL and we have nothing better
    #     to fall back to, ask the configured LLM what language the
    #     summary itself is in (one-shot, ~50 tokens).
    #   * summary_language follows the `summary_language` setting:
    #     "auto" / empty / None all mean "track the source language".
    if not source_lang:
        try:
            async def _complete(prompt: str) -> str:
                return await _completion(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    api_key=api_key or "",
                    base_url=base_url,
                )

            detected = await detect_language(summary, complete=_complete)
            if detected:
                source_lang = detected
        except Exception as e:  # noqa: BLE001 — best-effort fallback
            log.warning(
                "language detection failed for %s: %s: %s",
                video_id,
                type(e).__name__,
                e,
            )

    # Persist source_language explicitly when we have one — must
    # happen BEFORE set_summary so its COALESCE-on-summary backfill
    # doesn't shadow a detected source language that differs from
    # the explicit summary_language setting (e.g. detect="fr" but
    # summary_language="en"). When source_lang is None this is a
    # no-op and set_summary's COALESCE still handles the matching
    # auto case below.
    if source_lang:
        await videos_repo.set_source_language(db, video_id, source_lang)

    explicit = summary_language_setting and summary_language_setting != "auto"
    summary_lang = summary_language_setting if explicit else source_lang
    await videos_repo.set_summary(
        db, video_id, summary, model, language=summary_lang,
    )

    # Validate any inline [MM:SS](#t=SECONDS) links in the summary
    # against the segment inventory. Anomalies are logged but the
    # summary is NOT mutated — future iterations can decide policy.
    if segments:
        verified, anomalies = _verify_summary_timestamps(summary, segments)
        await set_step(
            f"timestamps verified — {verified} ok, {anomalies} anomalies"
        )
        if anomalies:
            log.warning(
                "video %s summary has %d timestamp anomalies "
                "(%d verified)",
                video_id,
                anomalies,
                verified,
            )

    await _try_embed_summary(db, video_id, summary, settings, set_step)

    # Curated related-summaries block (KNN pre-filter + LLM curation).
    # Runs AFTER embedding so this video's own vector is searchable, and
    # is best-effort: failure leaves related_links_json NULL.
    await set_step("finding related summaries")
    refreshed = await videos_repo.get(db, video_id)
    if refreshed is not None:
        await _store_related_links(
            db, video=refreshed, user_id=refreshed.user_id,
            model_row=model_row,
        )

    # Best-effort speaker detection (PR 2). Deterministic metadata match
    # only — no claims (the claim-extraction piggyback is PR 3). Mirrors
    # _store_related_links: gated, and never fails the job.
    if refreshed is not None:
        await set_step("identifying speakers")
        from app.services import speaker_pipeline
        await speaker_pipeline.detect_and_link(db, refreshed)


def _segments_for_summarizer(video) -> list[dict] | None:
    """Decode the JSON-stored transcript_segments into a list of
    {start, text} dicts suitable for `summarize()`. Only YouTube videos
    have a time concept — web articles and newsletters return None."""
    if video.kind != VideoKind.YOUTUBE:
        return None
    raw = getattr(video, "transcript_segments", None)
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start = item.get("start")
        text = (item.get("text") or "").strip()
        if start is None or not text:
            continue
        out.append({"start": float(start), "text": text})
    return out or None


async def _try_embed_summary(
    db: aiosqlite.Connection,
    video_id: str,
    summary: str,
    settings: dict[str, str],
    set_step: Callable[[str], Awaitable[None]],
) -> None:
    """Best-effort: embed the new summary so semantic search picks it up.

    A failure here is logged but does NOT fail the job — the user
    still has their summary, only semantic search is degraded.
    """
    # Embeddings run locally now (see app/services/embeddings_local.py);
    # the model/api_key/base_url params on embed_text are kept for
    # signature compatibility but ignored. The legacy llm_api_key /
    # llm_base_url reads were removed with the multi-model migration —
    # they would now resolve to empty strings anyway.
    embedding_model = settings.get("embedding_model", "").strip() or None
    embedding_base_url = settings.get("embedding_base_url", "").strip() or None
    try:
        await set_step("embedding summary")
        vector = await embed_text(
            summary,
            model=embedding_model,
            api_key="",
            base_url=embedding_base_url,
        )
        await embeddings_repo.upsert_summary_embedding(db, video_id, vector)
    except Exception as e:
        log.warning(
            "summary embedding failed for %s: %s: %s",
            video_id,
            type(e).__name__,
            e,
        )


async def _store_related_links(
    db, *, video, user_id: int, model_row,
) -> None:
    """Best-effort: compute + persist the curated related-links block.

    Never raises — related links are a nice-to-have and must not break
    the pipeline. On any failure the column stays NULL and the detail
    page falls back to the live-KNN strip.
    """
    try:
        links = await related_links.compute_related_links(
            db, video=video, user_id=user_id, model_row=model_row,
        )
        await videos_repo.set_related_links(
            db, video.id, json.dumps(links, ensure_ascii=False),
        )
    except Exception as e:  # noqa: BLE001 — best-effort, must not break
        log.warning(
            "related-links computation failed for %s: %s: %s",
            getattr(video, "id", None),
            type(e).__name__,
            e,
        )
