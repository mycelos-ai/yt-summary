"""CRUD for the llm_models table.

A single global registry of configured LLM "profiles" (provider + key
+ base_url + model id + human label). Exactly one row carries
``is_default=1`` (enforced by the partial unique index in SCHEMA).
The pipeline, worker, chat service and MCP server all resolve the
target model through this repo — no other code path should read the
old ``settings.llm_model`` keys (they no longer exist).
"""

from datetime import datetime

import aiosqlite

from app.models import LlmModel


def _row_to_model(row: aiosqlite.Row) -> LlmModel:
    return LlmModel(
        id=row["id"],
        label=row["label"],
        provider_id=row["provider_id"],
        model=row["model"],
        api_key=row["api_key"],
        base_url=row["base_url"],
        is_default=bool(row["is_default"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def get(db: aiosqlite.Connection, model_id: int) -> LlmModel | None:
    cursor = await db.execute(
        "SELECT * FROM llm_models WHERE id=?", (model_id,)
    )
    row = await cursor.fetchone()
    return _row_to_model(row) if row else None


async def get_default(db: aiosqlite.Connection) -> LlmModel | None:
    cursor = await db.execute(
        "SELECT * FROM llm_models WHERE is_default=1 LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_model(row) if row else None


async def list_all(db: aiosqlite.Connection) -> list[LlmModel]:
    """Return all configured models. Default row first, then alphabetical
    by label (case-insensitive). Empty list on a fresh install."""
    cursor = await db.execute(
        """
        SELECT * FROM llm_models
        ORDER BY is_default DESC, LOWER(label) ASC, id ASC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_model(r) for r in rows]


async def insert(
    db: aiosqlite.Connection,
    *,
    label: str,
    provider_id: str,
    model: str,
    api_key: str,
    base_url: str,
    make_default: bool,
) -> int:
    """Insert a new row. When ``make_default=True``, clears any existing
    default first so the partial unique index never fires."""
    if make_default:
        await db.execute("UPDATE llm_models SET is_default=0 WHERE is_default=1")
    cursor = await db.execute(
        """
        INSERT INTO llm_models
            (label, provider_id, model, api_key, base_url, is_default)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (label, provider_id, model, api_key, base_url, 1 if make_default else 0),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def update(
    db: aiosqlite.Connection,
    model_id: int,
    *,
    label: str,
    model: str,
    api_key: str,
    base_url: str,
) -> None:
    """Update the user-facing fields. is_default is NOT modified here —
    use set_default() for that to keep the invariant transactional."""
    await db.execute(
        """
        UPDATE llm_models
        SET label=?, model=?, api_key=?, base_url=?,
            updated_at=datetime('now')
        WHERE id=?
        """,
        (label, model, api_key, base_url, model_id),
    )
    await db.commit()


async def set_default(db: aiosqlite.Connection, model_id: int) -> None:
    """Flip the default flag onto ``model_id``. Two UPDATEs in one
    commit — single-writer SQLite makes this race-free."""
    await db.execute("UPDATE llm_models SET is_default=0 WHERE is_default=1")
    await db.execute(
        "UPDATE llm_models SET is_default=1, updated_at=datetime('now') WHERE id=?",
        (model_id,),
    )
    await db.commit()


async def delete(db: aiosqlite.Connection, model_id: int) -> None:
    """Delete a non-default row. Raises ValueError if the row is the
    current default — callers must move the default first."""
    row = await get(db, model_id)
    if row is None:
        return  # idempotent
    if row.is_default:
        raise ValueError(
            f"Cannot delete default model {model_id} — "
            "make another model default first."
        )
    await db.execute("DELETE FROM llm_models WHERE id=?", (model_id,))
    await db.commit()
