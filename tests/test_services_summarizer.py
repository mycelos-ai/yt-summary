"""Unit tests for the prompt builders. The summarize() function itself
hits litellm and is exercised by the integration tests; here we just
exercise pure-string assembly."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import model_info
from app.services.summarizer import build_reduce_prompt, build_system_prompt


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
    p = build_system_prompt(language="auto")
    assert "match the transcript" in p
    assert "TL;DR" in p
    assert "Mentioned resources" in p
    assert "Sponsor" in p


def test_build_system_prompt_with_explicit_language():
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="de")
    assert "German" in p


def test_build_system_prompt_with_custom_system_prompt_replaces_body():
    """Per-profile custom prompts replace the entire instructions
    block — only the language directive and the timestamp-format
    instruction stay wrapped around them."""
    from app.services.summarizer import build_system_prompt
    custom = "You are a pirate. Summarize like a pirate."
    p = build_system_prompt(language="en", custom_system_prompt=custom)
    assert custom in p
    # Standard body sections must NOT appear when a custom prompt
    # takes over.
    assert "TL;DR" not in p
    assert "THINK LIKE THE VIEWER" not in p
    # Language directive still wraps it.
    assert "English" in p


def test_build_system_prompt_falls_back_to_standard_when_no_custom():
    """When no custom prompt is given, the standard body is used —
    relevant for the boot path before the migration seeded prompts
    onto every profile."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language=None)
    assert "TL;DR" in p
    assert "THINK LIKE THE VIEWER" in p


def test_build_reduce_prompt_includes_language_and_resources():
    from app.services.summarizer import build_reduce_prompt
    p = build_reduce_prompt(language="en")
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
    p = build_system_prompt(language="auto")
    lower = p.lower()
    assert "specific" in lower or "concrete" in lower
    # Must explicitly call out announcements as a target
    assert "announce" in lower
    # And surface anti-pattern guidance
    assert "avoid" in lower or "do not paraphrase" in lower


def test_reduce_prompt_demands_specificity():
    from app.services.summarizer import build_reduce_prompt
    p = build_reduce_prompt(language="auto")
    lower = p.lower()
    assert "specific" in lower or "concrete" in lower
    assert "announce" in lower


def test_system_prompt_frames_viewer_already_committed():
    """The opening frame should anchor the LLM in the actual user intent:
    the reader has already decided the video is interesting, they just
    want to skip watching it. NOT a 'should I watch this?' decision."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="auto")
    # Frame-setting headline is intentionally caps-styled like the
    # other section headers in the prompt.
    assert "THINK LIKE THE VIEWER" in p
    # The committed-reader frame: the user has already decided the video
    # is interesting. Verifies the core stance, so a future edit doesn't
    # accidentally drift back to a "help me decide" frame.
    lower = p.lower()
    assert "already decided" in lower
    assert "saving" in lower and "time" in lower
    # The link to the inline-timestamps feature must be in the frame —
    # the timestamps are how we point the reader at moments worth
    # watching anyway.
    assert "timestamp" in lower


def test_system_prompt_marks_optional_sections_skip_silently():
    """Sections like Specifics / Quotes / Resources should be skip-able
    without the LLM writing acknowledgment sentences. This prevents
    'No product launches were announced' filler on educational videos."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="auto")
    lower = p.lower()
    # "Skip silently" or equivalent must appear so the LLM knows not
    # to render a placeholder when there's nothing to say.
    assert "skip silently" in lower or "skip silent" in lower
    # And the Specifics section header replaces the old
    # "Announcements / concrete claims" — verifies the rename happened.
    assert "**Specifics**" in p


def test_reduce_prompt_marks_optional_sections_skip_silently():
    from app.services.summarizer import build_reduce_prompt
    p = build_reduce_prompt(language="auto")
    lower = p.lower()
    assert "skip silently" in lower or "skip silent" in lower
    assert "**Specifics**" in p


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
    p = build_system_prompt(language="auto")
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
    p = build_reduce_prompt(language="auto")
    lower = p.lower()
    assert "title" in lower


# ---------------------------------------------------------------------------
# Inline timestamp links in summary
# ---------------------------------------------------------------------------


def test_build_system_prompt_includes_timestamp_instruction_when_segments_present():
    """When the summarizer is told there are transcript segments
    available, the system prompt MUST instruct the LLM to emit
    [MM:SS](#t=SECONDS) links for key moments."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(
        language="auto",
        with_timestamps=True,
    )
    # The exact format pattern the LLM must emit
    assert "[MM:SS](#t=SECONDS)" in p
    # Must reference a budget / cap so the model doesn't sprinkle
    assert "3" in p and ("7" in p or "high-value" in p.lower())
    # Must forbid invented timestamps
    assert "never invent" in p.lower() or "do not invent" in p.lower() \
        or "pick from" in p.lower()


def test_build_system_prompt_omits_timestamp_instruction_by_default():
    """No segments → no timestamp instruction. Web articles never have
    segments, so the prompt should stay clean for them."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(
        language="auto",
        with_timestamps=False,
    )
    assert "(#t=SECONDS)" not in p
    assert "[MM:SS]" not in p


def test_build_system_prompt_default_with_timestamps_is_off():
    """The new with_timestamps kwarg must default to False so existing
    callers that don't pass segments stay silent on the topic."""
    from app.services.summarizer import build_system_prompt
    p = build_system_prompt(language="auto")
    assert "(#t=SECONDS)" not in p


async def test_summarize_includes_segments_in_user_message():
    """When segments are passed, the user prompt body should list each
    one prefixed with its [MM:SS] timestamp so the model can reference
    real timestamps from the transcript."""
    from app.services.summarizer import summarize

    captured: dict = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return _completion_response("S")

    segments = [
        {"start": 0.0, "text": "Hello and welcome."},
        {"start": 42.5, "text": "Today we discuss specifics."},
        {"start": 135.0, "text": "Here is the punchline."},
    ]
    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=8000),
    ):
        await summarize(
            transcript="Hello and welcome. Today we discuss specifics. "
                       "Here is the punchline.",
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
            transcript_segments=segments,
        )

    user_msg = captured["messages"][1]["content"]
    sys_msg = captured["messages"][0]["content"]
    # User msg lists timestamps
    assert "[00:00]" in user_msg
    assert "[00:42]" in user_msg
    assert "[02:15]" in user_msg
    # System prompt picked up the timestamp instruction
    assert "(#t=SECONDS)" in sys_msg


async def test_summarize_omits_timestamp_instruction_when_no_segments():
    from app.services.summarizer import summarize

    captured: dict = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return _completion_response("S")

    with (
        patch("app.services.summarizer.litellm.acompletion", side_effect=fake_completion),
        patch("app.services.summarizer.litellm.token_counter", return_value=10),
        patch("app.services.model_info.litellm.get_max_tokens", return_value=8000),
    ):
        await summarize(
            transcript="some text",
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
            transcript_segments=None,
        )

    sys_msg = captured["messages"][0]["content"]
    user_msg = captured["messages"][1]["content"]
    assert "(#t=SECONDS)" not in sys_msg
    # No segment header sneaking into the user prompt either
    assert "[00:00]" not in user_msg


def test_verify_summary_timestamps_all_match():
    """Every [MM:SS](#t=N) in the summary corresponds to a real segment
    start (within ±5 s tolerance) → all verified, zero anomalies."""
    from app.services.summarizer import _verify_summary_timestamps

    segments = [
        {"start": 0.0, "text": "intro"},
        {"start": 60.0, "text": "middle"},
        {"start": 754.0, "text": "highlight"},
    ]
    summary = (
        "TL;DR.\n\n"
        "See [00:00](#t=0) for the intro and [12:34](#t=754) for the "
        "highlight."
    )
    verified, anomalies = _verify_summary_timestamps(summary, segments)
    assert verified == 2
    assert anomalies == 0


def test_verify_summary_timestamps_off_by_more_than_five_is_anomaly():
    from app.services.summarizer import _verify_summary_timestamps

    segments = [{"start": 0.0, "text": "intro"}, {"start": 100.0, "text": "x"}]
    # 200 has no neighbouring segment within 5 s → anomaly
    summary = "Look at [03:20](#t=200)."
    verified, anomalies = _verify_summary_timestamps(summary, segments)
    assert verified == 0
    assert anomalies == 1


def test_verify_summary_timestamps_within_five_seconds_counts_as_verified():
    from app.services.summarizer import _verify_summary_timestamps

    segments = [{"start": 100.0, "text": "x"}]
    # 104 is within 5s of 100 → verified
    summary = "[01:44](#t=104)"
    verified, anomalies = _verify_summary_timestamps(summary, segments)
    assert verified == 1
    assert anomalies == 0


def test_verify_summary_timestamps_empty_summary():
    from app.services.summarizer import _verify_summary_timestamps

    verified, anomalies = _verify_summary_timestamps("", [{"start": 0.0, "text": "x"}])
    assert verified == 0
    assert anomalies == 0


def test_verify_summary_timestamps_no_segments_treats_all_as_anomaly():
    """With no segments to validate against, every link is unverified."""
    from app.services.summarizer import _verify_summary_timestamps

    verified, anomalies = _verify_summary_timestamps(
        "[00:00](#t=0) [01:00](#t=60)", []
    )
    assert verified == 0
    assert anomalies == 2


def test_build_system_prompt_appends_additional_prompt_block():
    out = build_system_prompt(
        language="en",
        custom_system_prompt=None,
        with_timestamps=False,
        additional_prompt="be terse and quote dollar amounts",
    )
    assert "USER OVERRIDE FOR THIS RUN:" in out
    assert "be terse and quote dollar amounts" in out
    # Override block lives at the END so it overrides earlier
    # instructions in the model's attention budget.
    assert out.rstrip().endswith("be terse and quote dollar amounts")


def test_build_system_prompt_omits_block_when_no_override():
    out = build_system_prompt(
        language="en",
        custom_system_prompt=None,
        with_timestamps=False,
        additional_prompt=None,
    )
    assert "USER OVERRIDE FOR THIS RUN" not in out


def test_build_reduce_prompt_appends_additional_prompt_block():
    out = build_reduce_prompt(
        language="en",
        with_timestamps=False,
        additional_prompt="answer in bullet points only",
    )
    assert "USER OVERRIDE FOR THIS RUN:" in out
    assert "answer in bullet points only" in out
    # Override block must be terminal — same prompt-engineering
    # invariant as build_system_prompt's tests.
    assert out.rstrip().endswith("answer in bullet points only")


def test_build_system_prompt_with_custom_appends_override_block():
    """Custom-prompt branch must also pick up the override suffix.
    Two different code paths in build_system_prompt — both need it."""
    out = build_system_prompt(
        language="en",
        custom_system_prompt="My custom summary template.",
        with_timestamps=False,
        additional_prompt="be specific",
    )
    assert "USER OVERRIDE FOR THIS RUN:" in out
    assert "be specific" in out
    assert out.rstrip().endswith("be specific")
