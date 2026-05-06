from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import model_info


@pytest.fixture(autouse=True)
def _clear_model_info_cache():
    model_info.clear_cache()
    yield
    model_info.clear_cache()


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
        patch("app.services.model_info.litellm.get_max_tokens", return_value=8000),
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
        patch("app.services.model_info.litellm.get_max_tokens", return_value=2000),
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
        patch("app.services.model_info.litellm.get_max_tokens", return_value=8000),
    ):
        await summarize(
            transcript="t",
            model="openai/gpt-4o",
            api_key="k",
            base_url="https://my.proxy/v1",
        )
    assert captured["api_key"] == "k"
    assert captured["api_base"] == "https://my.proxy/v1"


async def test_summarize_calls_on_partial_per_chunk_in_map_reduce():
    from app.services.summarizer import summarize
    big = " ".join(["word"] * 100_000)

    partials_seen: list[str] = []

    async def collect_partial(text: str) -> None:
        partials_seen.append(text)

    async def fake_completion(**kwargs):
        return _completion_response("piece")

    def fake_token_counter(*, model: str, text: str) -> int:
        return len(text.split())

    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", side_effect=fake_token_counter),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=2000),
    ):
        await summarize(
            transcript=big,
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
            on_partial=collect_partial,
        )
    # At least 2 partial snapshots — every chunk except possibly the
    # final reduce step yields one
    assert len(partials_seen) >= 2
    # Each snapshot is a real string with the working-summary header
    for snap in partials_seen:
        assert "Working summary" in snap


async def test_summarize_does_not_call_on_partial_in_single_shot():
    from app.services.summarizer import summarize

    partials_seen: list[str] = []

    async def collect_partial(text: str) -> None:
        partials_seen.append(text)

    with (
        patch("app.services.summarizer.litellm.acompletion",
              AsyncMock(return_value=_completion_response("done"))),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=8000),
    ):
        await summarize(
            transcript="short",
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
            on_partial=collect_partial,
        )
    assert partials_seen == []


def test_build_system_prompt_with_auto_language():
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="auto", extra_instructions=None)
    assert "match the transcript" in p
    assert "TL;DR" in p
    assert "Mentioned resources" in p
    assert "Sponsor" in p


def test_build_system_prompt_with_explicit_language():
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="de", extra_instructions=None)
    assert "German" in p


def test_build_system_prompt_with_extra_instructions():
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(
        language=None, extra_instructions="Keep it under 200 words."
    )
    assert "ADDITIONAL USER INSTRUCTIONS" in p
    assert "Keep it under 200 words." in p


def test_build_system_prompt_omits_extra_block_when_empty():
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language=None, extra_instructions="   ")
    assert "ADDITIONAL USER INSTRUCTIONS" not in p


def test_build_reduce_prompt_includes_language_and_resources():
    from app.services.summarizer import build_reduce_prompt
    p = build_reduce_prompt(language="en", extra_instructions=None)
    assert "English" in p
    assert "Mentioned resources" in p


async def test_summarize_passes_title_and_description_to_user_message():
    from unittest.mock import AsyncMock, patch
    from app.services.summarizer import summarize

    captured: dict = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return _completion_response("ok")

    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=8000),
    ):
        await summarize(
            transcript="t",
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
            title="My Cool Video",
            description="Sponsored by ACME. Tools: https://example.com/foo",
            language="de",
        )

    user_msg = captured["messages"][1]["content"]
    sys_msg = captured["messages"][0]["content"]
    assert "My Cool Video" in user_msg
    assert "https://example.com/foo" in user_msg
    assert "German" in sys_msg
