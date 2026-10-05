from datetime import datetime

from app.models import Job, JobState, TranscriptSource
from app.services.job_progress import describe


def _job(step: str, started_at: datetime | None = None) -> Job:
    now = datetime(2026, 10, 5, 12, 0, 0)
    return Job(
        id=1, video_id="v1", state=JobState.RUNNING, step=step,
        error_message=None, created_at=now, updated_at=now,
        started_at=started_at,
    )


def _video(source: TranscriptSource | None = None) -> TranscriptSource | None:
    return source


def _chips(p):
    return [(ph.label, ph.state) for ph in p.phases]


def test_subtitle_check_is_neutral_until_result():
    p = describe(_job("checking subtitles"), _video())
    assert p.phases[0].label == "Transcript"
    assert p.headline == "Checking subtitles"
    assert p.fraction is None


def test_subtitles_found_labels_phase_subtitles():
    p = describe(_job("subtitles found (auto-generated)"), _video())
    assert _chips(p)[0] == ("Subtitles", "now")
    assert p.fraction == 1.0


def test_no_subtitles_switches_to_whisper():
    p = describe(_job("no subtitles, downloading audio for Whisper"), _video())
    assert p.label == "Whisper"
    assert p.headline == "Downloading audio"
    assert p.fraction is None


def test_local_whisper_progress_with_eta():
    p = describe(
        _job("transcribing 3:12 / 42:10 (8%) · ~12:40 left"), _video(),
    )
    assert p.label == "Whisper"
    assert p.headline == "3:12 / 42:10"
    assert p.percent == 8
    assert p.eta == "12:40"


def test_hosted_whisper_parts():
    p = describe(
        _job("sending audio to https://api.groq.com/openai/v1 (part 3/5)"),
        _video(),
    )
    assert p.label == "Whisper"
    assert p.headline == "Part 3 of 5"
    assert p.percent == 40


def test_summary_phase_keeps_transcript_source_as_done_chip():
    p = describe(_job("summarizing chunk 2/4"), _video(TranscriptSource.AUTO_SUBS))
    assert _chips(p) == [
        ("Subtitles", "done"), ("Summary", "now"),
        ("Linking", "todo"), ("Speakers", "todo"),
    ]
    assert p.headline == "Part 2 of 4"
    assert p.percent == 20


def test_single_shot_summary_is_indeterminate():
    p = describe(
        _job("summarizing (single-shot, ~9000 tokens, model context 128000)"),
        _video(TranscriptSource.WHISPER),
    )
    assert p.phases[0].label == "Whisper"
    assert p.fraction is None


def test_late_phases():
    p = describe(_job("embedding summary"), _video())
    assert p.label == "Linking"
    p = describe(_job("identifying speakers"), _video())
    assert [ph.state for ph in p.phases] == ["done", "done", "done", "now"]


def test_elapsed_counts_from_started_at():
    started = datetime(2026, 10, 5, 12, 0, 0)
    p = describe(
        _job("summarizing", started_at=started), _video(),
        now=datetime(2026, 10, 5, 12, 1, 30),
    )
    assert p.elapsed_s == 90


def test_unknown_step_still_renders():
    p = describe(_job("restarting after crash"), _video())
    assert p.headline == "restarting after crash"
    assert p.fraction is None
