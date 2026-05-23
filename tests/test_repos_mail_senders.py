from app.repos import mail_senders as repo


async def test_upsert_preserves_subscribed_flag(db):
    await repo.upsert_discovered(db, 1, [("a@x.com", "A", None, "Subj1")])
    await repo.set_subscriptions(db, 1, ["a@x.com"])
    # Re-discovery refreshes metadata but must NOT touch subscribed.
    await repo.upsert_discovered(db, 1, [("a@x.com", "A", "2026-01-01", "Subj2")])
    rows = await repo.list_for_user(db, 1)
    assert len(rows) == 1
    assert rows[0].subscribed is True
    assert rows[0].last_subject == "Subj2"


async def test_set_subscriptions_replaces_the_set(db):
    await repo.upsert_discovered(
        db, 1, [("a@x.com", "A", None, None), ("b@x.com", "B", None, None)]
    )
    await repo.set_subscriptions(db, 1, ["a@x.com"])
    assert await repo.subscribed_addrs(db, 1) == {"a@x.com"}
    # Switching the set unsubscribes the old one.
    await repo.set_subscriptions(db, 1, ["b@x.com"])
    assert await repo.subscribed_addrs(db, 1) == {"b@x.com"}


async def test_addresses_are_lowercased(db):
    await repo.upsert_discovered(db, 1, [("MixedCase@X.com", "M", None, None)])
    rows = await repo.list_for_user(db, 1)
    assert rows[0].sender_addr == "mixedcase@x.com"


async def test_senders_scoped_per_user(db):
    await repo.upsert_discovered(db, 1, [("a@x.com", "A", None, None)])
    await repo.upsert_discovered(db, 2, [("b@x.com", "B", None, None)])
    await repo.set_subscriptions(db, 1, ["a@x.com"])
    assert await repo.subscribed_addrs(db, 2) == set()
    assert {s.sender_addr for s in await repo.list_for_user(db, 2)} == {"b@x.com"}
