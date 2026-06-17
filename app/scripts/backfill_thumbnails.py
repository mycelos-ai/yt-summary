"""Backfill Pexels stock thumbnails for email/web items.

Usage:
  python -m app.scripts.backfill_thumbnails [--force] [--dry-run]
                                            [--user-id N] [--limit N]

--force      re-fetch even items that already have a thumbnail
--dry-run    resolve image queries and log them; no Pexels call, no write
--user-id N  restrict to one profile
--limit N    process at most N items
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.config import Config
from app.db import connect, init_schema
from app.repos import llm_models as llm_models_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services import stock_images

log = logging.getLogger("backfill_thumbnails")


async def run_backfill(
    db, config: Config, *, api_key: str, force: bool, dry_run: bool,
    user_id: int | None = None, limit: int | None = None,
    pause_s: float = 0.3,
) -> dict[str, int]:
    summary = {
        "checked": 0, "query_generated": 0, "fetched": 0,
        "no_result": 0, "skipped": 0, "error": 0,
    }
    model_row = await llm_models_repo.get_default(db)
    videos = await videos_repo.list_for_thumbnail_backfill(
        db, user_id=user_id, only_missing=False, limit=limit,
    )
    for video in videos:
        summary["checked"] += 1
        # Skip items that already have a thumbnail unless --force was given.
        if video.thumbnail_path and not force:
            summary["skipped"] += 1
            continue
        query = (video.image_query or "").strip()
        if not query:
            generated = await stock_images.generate_image_query(
                summary=video.summary or "", model_row=model_row,
            )
            if generated:
                query = generated
                if not dry_run:
                    await videos_repo.set_image_query(db, video.id, generated)
                summary["query_generated"] += 1
            else:
                summary["skipped"] += 1
                continue
        if dry_run:
            log.info("[dry-run] %s -> %r", video.id, query)
            continue
        # Re-fetch so ensure_* sees the freshly-written query.
        refreshed = await videos_repo.get(db, video.id)
        if refreshed is None:
            summary["error"] += 1
            continue
        try:
            changed = await stock_images.ensure_stock_thumbnail(
                db, refreshed, config=config, api_key=api_key, force=force,
            )
        except Exception as e:  # pragma: no cover - defensive
            log.info("fetch failed for %s: %s", video.id, e)
            summary["error"] += 1
            continue
        if changed:
            summary["fetched"] += 1
        else:
            summary["no_result"] += 1
        if pause_s:
            await asyncio.sleep(pause_s)
    return summary


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = Config.from_env()
    config.ensure_dirs()
    db = await connect(config)
    await init_schema(db)
    try:
        api_key = ""
        if args.user_id is not None:
            api_key = await settings_repo.get_for_user(
                db, args.user_id, "pexels_api_key",
            ) or ""
        else:
            api_key = await settings_repo.get(db, "pexels_api_key") or ""
        if not api_key and not args.dry_run:
            log.info(
                "No pexels_api_key configured — nothing to do. "
                "(Use --dry-run to preview queries.)"
            )
            return
        result = await run_backfill(
            db, config, api_key=api_key, force=args.force,
            dry_run=args.dry_run, user_id=args.user_id, limit=args.limit,
        )
        log.info("Backfill summary: %s", result)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
