async def test_detect_language_returns_two_letter_code():
    """detect_language passes a short prompt asking for an ISO code."""
    from app.services.language_detect import detect_language

    seen: list[str] = []

    async def fake_complete(prompt: str) -> str:
        seen.append(prompt)
        return "de"

    code = await detect_language(
        "Das ist ein Test der Spracherkennung.",
        complete=fake_complete,
    )
    assert code == "de"
    assert "ISO 639-1" in seen[0]


async def test_detect_language_normalises_response():
    """LLMs sometimes wrap the answer in quotes or add a period.
    Strip those down to the bare code."""
    from app.services.language_detect import detect_language

    async def fake_complete(_: str) -> str:
        return '"DE".'

    code = await detect_language("…", complete=fake_complete)
    assert code == "de"


async def test_detect_language_returns_none_on_unrecognised():
    from app.services.language_detect import detect_language

    async def fake_complete(_: str) -> str:
        return "Sorry I cannot answer that."

    assert await detect_language("…", complete=fake_complete) is None
