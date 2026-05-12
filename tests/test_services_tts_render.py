"""Integration tests for the Piper -> ffmpeg MP3 render pipeline.

These tests are real integration tests: they invoke Piper and ffmpeg
under the hood, so they take a few seconds each. They depend on the
session-scoped ``amy_low_voice`` fixture in ``tests/conftest.py``,
which caches the Piper voice under ``~/.cache/yt-summary-test-voices/``.
"""
from pathlib import Path

import pytest


async def test_render_text_to_mp3_produces_valid_mp3(
    tmp_path: Path, amy_low_voice: Path
) -> None:
    """Smoke test: feed a short sentence into the en_US-amy-low
    voice and verify we get a non-empty MP3 file back."""
    from app.services.tts_render import render_text_to_mp3

    out = tmp_path / "out.mp3"
    await render_text_to_mp3(
        text="This is a test of the speech synthesis pipeline.",
        voice_file=amy_low_voice,
        out_path=out,
    )
    assert out.exists()
    assert out.stat().st_size > 1000
    # MP3 magic: starts with ID3 tag or 0xFF sync byte
    head = out.read_bytes()[:3]
    assert head[:3] == b"ID3" or head[0] == 0xFF


async def test_render_concatenates_multiple_chunks(
    tmp_path: Path, amy_low_voice: Path
) -> None:
    """A list of texts produces a single MP3 with combined duration."""
    from app.services.tts_render import render_chunks_to_mp3, render_text_to_mp3

    single = tmp_path / "single.mp3"
    multi = tmp_path / "multi.mp3"
    await render_text_to_mp3("Hello world.", amy_low_voice, single)
    await render_chunks_to_mp3(
        chunks=["Hello world.", "Hello world.", "Hello world."],
        voice_file=amy_low_voice,
        out_path=multi,
    )
    # Multi must be substantially larger than single (3x roughly).
    assert multi.stat().st_size > 2.5 * single.stat().st_size


async def test_render_chunks_empty_list_raises(
    tmp_path: Path, amy_low_voice: Path
) -> None:
    """An empty chunks list must raise ValueError upfront, not invoke
    ffmpeg with an empty manifest (which produces a misleading error)."""
    from app.services.tts_render import render_chunks_to_mp3

    out = tmp_path / "empty.mp3"
    with pytest.raises(ValueError, match="at least one chunk"):
        await render_chunks_to_mp3(chunks=[], voice_file=amy_low_voice, out_path=out)
    assert not out.exists()


async def test_render_chunks_single_chunk_produces_valid_mp3(
    tmp_path: Path, amy_low_voice: Path
) -> None:
    """A one-chunk list must short-circuit through the plain WAV->MP3
    path and produce a valid MP3 (avoids the ffmpeg single-input
    concat-demuxer edge case)."""
    from app.services.tts_render import render_chunks_to_mp3

    out = tmp_path / "one.mp3"
    await render_chunks_to_mp3(
        chunks=["This is a single-chunk render."],
        voice_file=amy_low_voice,
        out_path=out,
    )
    assert out.exists()
    assert out.stat().st_size > 1000
    head = out.read_bytes()[:3]
    assert head[:3] == b"ID3" or head[0] == 0xFF


async def test_render_respects_length_scale(
    tmp_path: Path, amy_low_voice: Path
) -> None:
    """``length_scale`` > 1.0 must produce a longer (= larger MP3 at
    128 kbps CBR) render than ``length_scale`` < 1.0 for the same
    input text. This pins down that the Piper SynthesisConfig is
    actually being passed through, not silently dropped on the floor."""
    from app.services.tts_render import render_text_to_mp3

    fast = tmp_path / "fast.mp3"
    slow = tmp_path / "slow.mp3"
    text = "Hello world, this is a test of speech rate."
    await render_text_to_mp3(text, amy_low_voice, fast, length_scale=0.7)
    await render_text_to_mp3(text, amy_low_voice, slow, length_scale=1.5)
    # Slow rendering should produce at least 1.4x the bytes of fast.
    # Both files share the same MP3 header overhead; at 128 kbps CBR
    # the difference scales near-linearly with audio duration.
    assert slow.stat().st_size > 1.4 * fast.stat().st_size
