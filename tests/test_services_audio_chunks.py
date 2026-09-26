import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _make_tone(path, seconds: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}",
            "-c:a", "aac", str(path),
        ],
        check=True,
    )


async def test_extract_chunk_cuts_exact_lengths_until_end(tmp_path):
    from app.services.audio_chunks import extract_chunk
    from app.services.audio_probe import probe_duration

    src = tmp_path / "src.m4a"
    _make_tone(src, 25)

    lengths = []
    for i in range(4):
        chunk = await extract_chunk(
            src, tmp_path / "chunks" / f"chunk_{i:03d}.flac",
            start_s=i * 10, length_s=10,
        )
        if chunk is None:
            break
        lengths.append(round(await probe_duration(chunk)))
    assert lengths == [10, 10, 5]
    assert not (tmp_path / "chunks" / "chunk_003.flac").exists()


async def test_extract_chunk_raises_on_bad_input(tmp_path):
    from app.services.audio_chunks import extract_chunk

    src = tmp_path / "broken.m4a"
    src.write_bytes(b"not audio")
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await extract_chunk(src, tmp_path / "c.flac", start_s=0, length_s=10)
