import aiosqlite
import pytest

from app.repos import llm_models as repo


async def test_insert_first_row_can_be_default(db: aiosqlite.Connection):
    new_id = await repo.insert(
        db,
        label="Claude",
        provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-xxx",
        base_url="",
        make_default=True,
    )
    row = await repo.get(db, new_id)
    assert row is not None
    assert row.label == "Claude"
    assert row.is_default is True


async def test_get_default_returns_none_when_empty(db: aiosqlite.Connection):
    assert await repo.get_default(db) is None


async def test_get_default_returns_the_default_row(db: aiosqlite.Connection):
    await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    second = await repo.insert(
        db, label="B", provider_id="ollama", model="ollama_chat/llama3.1",
        api_key="", base_url="http://lan:11434", make_default=False,
    )
    default = await repo.get_default(db)
    assert default is not None
    assert default.label == "A"
    row = await repo.get(db, second)
    assert row is not None and row.is_default is False


async def test_set_default_flips_atomically(db: aiosqlite.Connection):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    b = await repo.insert(
        db, label="B", provider_id="ollama", model="ollama_chat/llama3.1",
        api_key="", base_url="http://lan:11434", make_default=False,
    )
    await repo.set_default(db, b)
    default = await repo.get_default(db)
    assert default is not None and default.id == b
    row_a = await repo.get(db, a)
    assert row_a is not None and row_a.is_default is False


async def test_list_all_orders_default_first_then_label(db: aiosqlite.Connection):
    await repo.insert(
        db, label="Zeta",  provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=False,
    )
    await repo.insert(
        db, label="Alpha", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="k", base_url="", make_default=True,
    )
    await repo.insert(
        db, label="Beta",  provider_id="groq",
        model="groq/llama-3.3-70b-versatile",
        api_key="k", base_url="", make_default=False,
    )
    rows = await repo.list_all(db)
    assert [r.label for r in rows] == ["Alpha", "Beta", "Zeta"]


async def test_update_changes_fields_in_place(db: aiosqlite.Connection):
    rid = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    await repo.update(
        db, rid,
        label="A renamed",
        model="openai/gpt-5.4",
        api_key="new-key",
        base_url="",
    )
    row = await repo.get(db, rid)
    assert row is not None
    assert row.label == "A renamed"
    assert row.model == "openai/gpt-5.4"
    assert row.api_key == "new-key"
    assert row.is_default is True


async def test_delete_non_default_row(db: aiosqlite.Connection):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    b = await repo.insert(
        db, label="B", provider_id="ollama",
        model="ollama_chat/llama3.1", api_key="", base_url="x",
        make_default=False,
    )
    await repo.delete(db, b)
    assert await repo.get(db, b) is None
    assert await repo.get(db, a) is not None


async def test_delete_default_row_raises(db: aiosqlite.Connection):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    with pytest.raises(ValueError):
        await repo.delete(db, a)
    assert await repo.get(db, a) is not None


async def test_set_default_raises_on_unknown_id(db: aiosqlite.Connection):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    with pytest.raises(ValueError):
        await repo.set_default(db, 9999)
    # The original default must still be the default — the table can
    # never end up with zero defaults from a failed set_default call.
    default = await repo.get_default(db)
    assert default is not None and default.id == a


async def test_update_raises_on_unknown_id(db: aiosqlite.Connection):
    with pytest.raises(ValueError):
        await repo.update(
            db, 9999,
            label="X", model="x/y", api_key="", base_url="",
        )


async def test_list_all_returns_empty_on_fresh_install(db: aiosqlite.Connection):
    assert await repo.list_all(db) == []


async def test_insert_with_make_default_displaces_prior_default(
    db: aiosqlite.Connection,
):
    a = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    b = await repo.insert(
        db, label="B", provider_id="anthropic",
        model="anthropic/claude-sonnet-4-6",
        api_key="k", base_url="", make_default=True,
    )
    default = await repo.get_default(db)
    assert default is not None and default.id == b
    row_a = await repo.get(db, a)
    assert row_a is not None and row_a.is_default is False


async def test_update_advances_updated_at(db: aiosqlite.Connection):
    import asyncio

    rid = await repo.insert(
        db, label="A", provider_id="openai", model="openai/gpt-5.5",
        api_key="k", base_url="", make_default=True,
    )
    before = await repo.get(db, rid)
    assert before is not None
    # SQLite's datetime('now') resolution is one second. Sleep just
    # over a second so the stamp definitely advances.
    await asyncio.sleep(1.05)
    await repo.update(
        db, rid,
        label="A renamed", model="openai/gpt-5.4",
        api_key="k", base_url="",
    )
    after = await repo.get(db, rid)
    assert after is not None
    assert after.updated_at > before.updated_at
