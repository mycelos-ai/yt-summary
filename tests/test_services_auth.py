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
