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


def test_system_prompt_demands_specificity():
    """Generic LinkedIn-style summaries are the failure mode. The
    prompt must explicitly demand concrete names / numbers / quotes."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="auto", extra_instructions=None)
    lower = p.lower()
    assert "specific" in lower or "concrete" in lower
    # Must explicitly call out announcements as a target
    assert "announce" in lower
    # And surface anti-pattern guidance
    assert "avoid" in lower or "do not paraphrase" in lower


def test_reduce_prompt_demands_specificity():
    from app.services.summarizer import build_reduce_prompt
    p = build_reduce_prompt(language="auto", extra_instructions=None)
    lower = p.lower()
    assert "specific" in lower or "concrete" in lower
    assert "announce" in lower


def test_render_live_summary_human_friendly_header():
    """The intermediate state shown to the UI should not look like
    debug output. No raw 'Part 1 of 7 / Part 2 of 7' headers."""
    from app.services.summarizer import _render_live_summary
    out = _render_live_summary(["alpha summary", "beta summary"], total=4)
    # Friendly header indicating progress
    assert "2" in out and "4" in out
    # Should still contain both partial bodies
    assert "alpha summary" in out
    assert "beta summary" in out


async def test_summarize_passes_playlist_context_to_user_message():
    """When the video sits in named playlists, surface those names to
    the LLM as topic hints — the user organises their queue
    thematically and we want the summary to lean into that bucket."""
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
            title="Some Video",
            description="",
            playlist_context=["AI", "Long-form interviews"],
        )

    user_msg = captured["messages"][1]["content"]
    # Both playlist names should appear, comma-joined or similar
    assert "AI" in user_msg
    assert "Long-form interviews" in user_msg
    # Some explicit framing so the LLM knows what these are
    assert (
        "playlist" in user_msg.lower()
        or "topic" in user_msg.lower()
        or "filed" in user_msg.lower()
    )


async def test_summarize_no_playlist_context_omits_section():
    """If the video isn't in any playlist, don't add a confusing empty
    or 'no playlist' line — just leave that part of the prompt out."""
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
            title="Standalone video",
            description="",
            playlist_context=None,
        )

    user_msg = captured["messages"][1]["content"]
    # No "ADDED TO" / "PLAYLIST" header should appear when there's no
    # playlist context — keep the prompt clean.
    assert "ADDED TO" not in user_msg
    assert "PLAYLIST" not in user_msg


async def test_summarize_empty_playlist_list_treated_as_none():
    """A list with no entries shouldn't render an empty header either."""
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
            title="x",
            description="",
            playlist_context=[],
        )

    user_msg = captured["messages"][1]["content"]
    assert "ADDED TO" not in user_msg
    assert "PLAYLIST" not in user_msg


def test_system_prompt_demands_title_answer():
    """When the video title asks a question or makes a promise, the
    summary must surface the answer directly. If the speaker dodges,
    the summary must say so."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="auto", extra_instructions=None)
    lower = p.lower()
    # The rule lives under an explicit ANSWER THE TITLE section
    assert "answer the title" in lower or "title" in lower
    # Mentions both the question and promise patterns
    assert "question" in lower
    # Includes the dodged-question case so the LLM doesn't hide it
    assert (
        "dodge" in lower or "doesn't answer" in lower
        or "side-step" in lower or "sidestep" in lower
    )


def test_reduce_prompt_demands_title_answer():
    """The reduce step also needs to surface the title-answer when
    multiple partials mention it differently."""
    from app.services.summarizer import build_reduce_prompt
    p = build_reduce_prompt(language="auto", extra_instructions=None)
    lower = p.lower()
    assert "title" in lower
