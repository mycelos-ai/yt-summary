from unittest.mock import AsyncMock, MagicMock, patch


def _completion_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.message.content = text
    response = MagicMock()
    response.choices = [msg]
    return response


async def test_summarize_single_shot_when_fits():
    from app.services.summarizer import summarize
    transcript = "short transcript"

    with (
        patch("app.services.summarizer.litellm.acompletion",
              AsyncMock(return_value=_completion_response("the summary"))),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.summarizer.litellm.get_max_tokens", return_value=8000),
    ):
        result = await summarize(
            transcript=transcript,
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
        )
    assert result == "the summary"


async def test_summarize_map_reduce_when_too_large():
    from app.services.summarizer import summarize
    big = " ".join(["word"] * 100_000)

    calls = {"n": 0, "messages": []}

    async def fake_completion(**kwargs):
        calls["n"] += 1
        calls["messages"].append(kwargs["messages"])
        return _completion_response(f"chunk-{calls['n']}")

    def fake_token_counter(*, model: str, text: str) -> int:
        return len(text.split())

    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", side_effect=fake_token_counter),
        patch("app.services.summarizer.litellm.get_max_tokens", return_value=2000),
    ):
        result = await summarize(
            transcript=big,
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
        )
    assert calls["n"] >= 2
    assert len(result) > 0


async def test_summarize_passes_base_url_when_set():
    from app.services.summarizer import summarize
    captured = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return _completion_response("x")

    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.summarizer.litellm.get_max_tokens", return_value=8000),
    ):
        await summarize(
            transcript="t",
            model="openai/gpt-4o",
            api_key="k",
            base_url="https://my.proxy/v1",
        )
    assert captured["api_key"] == "k"
    assert captured["api_base"] == "https://my.proxy/v1"
