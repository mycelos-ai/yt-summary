from unittest.mock import MagicMock


def test_generate_api_key_returns_three_pieces():
    from app.services.auth import generate_api_key
    plaintext, key_hash, prefix = generate_api_key()
    assert isinstance(plaintext, str)
    assert plaintext.startswith("yts_")
    assert len(plaintext) > 30
    assert isinstance(key_hash, str)
    assert len(key_hash) == 64  # sha256 hex
    assert prefix == plaintext[:8]


def test_generate_api_key_is_random():
    from app.services.auth import generate_api_key
    a, _, _ = generate_api_key()
    b, _, _ = generate_api_key()
    assert a != b


def test_hash_api_key_is_deterministic():
    from app.services.auth import hash_api_key
    h1 = hash_api_key("yts_abc123")
    h2 = hash_api_key("yts_abc123")
    assert h1 == h2
    assert len(h1) == 64


async def _fake_request(headers: dict[str, str]) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    return req


async def test_authenticate_returns_default_user_when_no_key_configured(db):
    from app.services.auth import authenticate
    req = await _fake_request({})
    user_id = await authenticate(db, req)
    assert user_id == 1


async def test_authenticate_with_valid_bearer(db):
    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    plaintext = "yts_test123abc"
    await users_repo.set_api_key(
        db, user_id=1, key_hash=hash_api_key(plaintext), key_prefix=plaintext[:8]
    )
    req = await _fake_request({"authorization": f"Bearer {plaintext}"})
    user_id = await authenticate(db, req)
    assert user_id == 1


async def test_authenticate_with_valid_x_api_key(db):
    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    plaintext = "yts_xyzqwerty"
    await users_repo.set_api_key(
        db, user_id=1, key_hash=hash_api_key(plaintext), key_prefix=plaintext[:8]
    )
    req = await _fake_request({"x-api-key": plaintext})
    user_id = await authenticate(db, req)
    assert user_id == 1


async def test_authenticate_rejects_wrong_key(db):
    import pytest
    from fastapi import HTTPException

    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    await users_repo.set_api_key(
        db,
        user_id=1,
        key_hash=hash_api_key("yts_correct"),
        key_prefix="yts_corr",
    )
    req = await _fake_request({"authorization": "Bearer yts_wrong"})
    with pytest.raises(HTTPException) as exc:
        await authenticate(db, req)
    assert exc.value.status_code == 401


async def test_authenticate_rejects_missing_when_key_required(db):
    import pytest
    from fastapi import HTTPException

    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    await users_repo.set_api_key(
        db,
        user_id=1,
        key_hash=hash_api_key("yts_yes"),
        key_prefix="yts_yes_",
    )
    req = await _fake_request({})
    with pytest.raises(HTTPException) as exc:
        await authenticate(db, req)
    assert exc.value.status_code == 401


async def test_authenticate_uses_constant_time_compare(db, monkeypatch):
    """The presented-vs-stored hash comparison must go through
    hmac.compare_digest, not a plain `!=` whose early-return leaks the
    matching-prefix length via timing."""
    import app.services.auth as auth_mod
    from app.repos import users as users_repo
    from app.services.auth import authenticate, hash_api_key
    plaintext = "yts_consttime"
    await users_repo.set_api_key(
        db, user_id=1,
        key_hash=hash_api_key(plaintext), key_prefix=plaintext[:8],
    )

    calls: list[tuple[str, str]] = []
    real_compare = auth_mod.hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", _spy)
    req = await _fake_request({"authorization": f"Bearer {plaintext}"})
    user_id = await authenticate(db, req)
    assert user_id == 1
    assert calls, "authenticate must compare the key with hmac.compare_digest"
    assert real_compare(*calls[0])  # the two hashes it compared do match
