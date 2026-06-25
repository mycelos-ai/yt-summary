import asyncio

from app.repos import known_shows as repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_list_enabled_orders_user_before_seed(db):
    """USER-defined show rules must precede SEEDED (user_id IS NULL) rules.

    Insert order is SEED first so that a no-ORDER-BY implementation would
    (under typical SQLite rowid ordering) return the seed row first, making
    the test RED before the fix.
    """

    async def go():
        # Insert SEED row first (user_id NULL)
        await db.execute(
            "INSERT INTO known_shows (user_id, name, title_pattern, hosts_json, enabled) "
            "VALUES (NULL, 'All-In Seed', 'All-In Summit', '[\"Chamath\"]', 1)"
        )
        # Insert USER row second (user_id = 1)
        await db.execute(
            "INSERT INTO known_shows (user_id, name, title_pattern, hosts_json, enabled) "
            "VALUES (1, 'All-In User', 'All-In Summit', '[\"Chamath\",\"Jason\"]', 1)"
        )
        await db.commit()

        result = await repo.list_enabled(db, user_id=1)

        # Find the positions of our specific inserted rows
        user_idx = next(
            (i for i, r in enumerate(result) if r.name == "All-In User"), None
        )
        seed_idx = next(
            (i for i, r in enumerate(result) if r.name == "All-In Seed"), None
        )
        assert user_idx is not None, "User row 'All-In User' not found in results"
        assert seed_idx is not None, "Seed row 'All-In Seed' not found in results"
        # USER row must come before SEED row
        assert user_idx < seed_idx, (
            f"Expected user row (pos {user_idx}) before seed row (pos {seed_idx})"
        )

    _run(go())


def test_list_enabled_stable_id_order_within_same_tier(db):
    """Within the same tier (seed or user), rows are ordered by id ascending."""

    async def go():
        # Insert two seed rows — higher id inserted first in SQL order
        await db.execute(
            "INSERT INTO known_shows (user_id, name, title_pattern, hosts_json, enabled) "
            "VALUES (NULL, 'Seed A', 'patternA', '[]', 1)"
        )
        await db.execute(
            "INSERT INTO known_shows (user_id, name, title_pattern, hosts_json, enabled) "
            "VALUES (NULL, 'Seed B', 'patternB', '[]', 1)"
        )
        await db.commit()

        result = await repo.list_enabled(db, user_id=1)

        # Filter to just our two inserted rows to avoid interference from seeded rows
        our_rows = [r for r in result if r.name in ("Seed A", "Seed B")]
        assert len(our_rows) == 2, f"Expected our 2 rows, found {len(our_rows)}"
        # IDs must be ascending (stable secondary sort within same tier)
        ids = [r.id for r in our_rows]
        assert ids == sorted(ids), f"Expected ascending id order, got {ids}"

    _run(go())
