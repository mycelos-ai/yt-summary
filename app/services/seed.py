"""Idempotent, version-gated seed loaders for known_shows and known_speakers.

Design notes
------------
- UPSERT, never wipe. A ``DELETE FROM known_speakers`` would raise
  "FOREIGN KEY constraint failed" the moment any profile ``speakers`` row has
  ``known_speaker_id`` referencing a seeded row (a nullable FK is NOT the
  same as ``ON DELETE SET NULL`` — SQLite still enforces the constraint on
  DELETE). The upsert on the natural key (``known_shows.name`` via a partial
  UNIQUE index; ``known_speakers.name_key`` via its table UNIQUE) lets a
  version bump re-import cleanly without ever removing a parent row.

- Version gating: each JSON file carries a ``version`` integer. The loader
  reads a ``settings`` marker (``known_shows_seed_version`` /
  ``known_speakers_seed_version``) and returns immediately if it already
  equals the file version. Bumping the version in the JSON triggers a full
  re-import on next boot.

- Key extraction is EXPLICIT (``items_key = "shows" if "shows" in payload
  else "speakers"``). Do NOT use ``payload.keys() & {…}`` — a set is not
  subscriptable and set ordering is not a contract.
"""

import json
from pathlib import Path

import aiosqlite

from app.repos import settings as settings_repo

_DATA = Path(__file__).resolve().parent.parent / "data"


async def _seed(
    db: aiosqlite.Connection,
    *,
    file: str,
    marker: str,
    insert,
) -> None:
    payload = json.loads((_DATA / file).read_text(encoding="utf-8"))
    version = str(payload.get("version", 1))
    if await settings_repo.get(db, marker) == version:
        return
    # Explicit key — ``payload.keys() & {...}`` returns a set, which is NOT
    # subscriptable (and set-ordering is not a contract). Pick the one real key.
    items_key = "shows" if "shows" in payload else "speakers"
    for item in payload[items_key]:
        await insert(db, item, version)
    await settings_repo.set(db, marker, version)
    await db.commit()


async def seed_known_shows(db: aiosqlite.Connection) -> None:
    """Upsert seeded shows into known_shows.

    Conflict target: ``(name) WHERE user_id IS NULL`` — requires the partial
    unique index ``uq_known_shows_seed_name`` (added to SCHEMA in db.py).
    """
    async def ins(db: aiosqlite.Connection, s: dict, version: str) -> None:
        await db.execute(
            "INSERT INTO known_shows "
            "(user_id, name, channel_id, title_pattern, description_pattern, hosts_json, guest_rule, seed_version) "
            "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) WHERE user_id IS NULL DO UPDATE SET "
            "channel_id=excluded.channel_id, "
            "title_pattern=excluded.title_pattern, "
            "description_pattern=excluded.description_pattern, "
            "hosts_json=excluded.hosts_json, "
            "guest_rule=excluded.guest_rule, "
            "seed_version=excluded.seed_version",
            (
                s["name"],
                s.get("channel_id"),
                s.get("title_pattern"),
                s.get("description_pattern"),
                json.dumps(s.get("hosts", [])),
                s.get("guest_rule"),
                int(version),
            ),
        )

    await _seed(
        db,
        file="known_shows.json",
        marker="known_shows_seed_version",
        insert=ins,
    )


async def seed_known_speakers(db: aiosqlite.Connection) -> None:
    """Upsert seeded speakers into known_speakers.

    Conflict target: ``name_key`` — already UNIQUE on the table.
    ``name_key`` is computed with the same normalisation as
    ``app.repos.speakers.normalize_name_key``.
    """
    async def ins(db: aiosqlite.Connection, s: dict, version: str) -> None:
        await db.execute(
            "INSERT INTO known_speakers "
            "(name, name_key, role, known_shows, avatar_id, style_note, seed_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name_key) DO UPDATE SET "
            "name=excluded.name, "
            "role=excluded.role, "
            "known_shows=excluded.known_shows, "
            "avatar_id=excluded.avatar_id, "
            "style_note=excluded.style_note, "
            "seed_version=excluded.seed_version",
            (
                s["name"],
                _key(s["name"]),
                s.get("role"),
                s.get("known_shows"),
                s.get("avatar_id"),
                s.get("style_note"),
                int(version),
            ),
        )

    await _seed(
        db,
        file="known_speakers.json",
        marker="known_speakers_seed_version",
        insert=ins,
    )


def _key(name: str) -> str:
    """Return the normalized name_key for a speaker name.

    Delegates to ``app.repos.speakers.normalize_name_key`` so both paths
    use the same normalisation logic.
    """
    from app.repos.speakers import normalize_name_key
    return normalize_name_key(name)
