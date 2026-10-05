"""Turn a running job's free-text step into the progress ring's data.

The pipeline reports progress as human-readable strings in `jobs.step`
(see app/pipeline.py, app/services/transcript.py and
app/services/summarizer.py). This module maps those strings onto four
phases so the detail page can draw a ring for the current phase and a
chip per phase. Strings it does not recognise still render: the ring
spins and the raw step is shown as the detail line.

The transcript phase is labelled by how the transcript is obtained.
Subtitles are checked before any audio is downloaded, so the Whisper
label only appears once the subtitle check has come back empty.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models import Job, TranscriptSource, Video

_TRANSCRIBING = re.compile(
    r"transcribing (?P<cur>[\d:]+)(?: / (?P<tot>[\d:]+) \((?P<pct>\d+)%\))?"
    r"(?: · ~(?P<eta>[\d:]+) left)?"
)
_PART = re.compile(r"\(part (\d+)/(\d+)\)")
_CHUNK = re.compile(r"summarizing chunk (\d+)/(\d+)")
_MERGING = re.compile(r"merging (\d+) partial summaries")

_SUMMARY_STEPS = ("summarizing", "merging")
_INDEX_STEPS = ("timestamps verified", "embedding summary", "finding related")
_SPEAKER_STEPS = ("identifying speakers",)


@dataclass(frozen=True)
class Phase:
    key: str
    label: str
    state: str  # 'done' | 'now' | 'todo'


@dataclass(frozen=True)
class JobProgress:
    phases: list[Phase]
    label: str  # current phase, shown above the big line
    headline: str  # big line next to the ring, e.g. "3:12 / 42:10"
    fraction: float | None  # 0..1 for the ring; None = indeterminate
    eta: str | None  # e.g. "1:20"
    elapsed_s: int | None  # since the worker claimed the job

    @property
    def percent(self) -> int | None:
        return None if self.fraction is None else int(round(self.fraction * 100))


def _transcript_label(step: str, video: Video) -> str:
    if "subtitles found" in step or "checking subtitles" in step:
        return "Subtitles" if "found" in step else "Transcript"
    if (
        "whisper" in step.lower()
        or step.startswith("transcribing")
        or step.startswith("sending audio")
    ):
        return "Whisper"
    if "reused" in step:
        return "Transcript"
    if "fetching article" in step:
        return "Article"
    source = video.transcript_source
    if source in (TranscriptSource.MANUAL_SUBS, TranscriptSource.AUTO_SUBS):
        return "Subtitles"
    if source == TranscriptSource.WHISPER:
        return "Whisper"
    if source == TranscriptSource.WEB:
        return "Article"
    if source == TranscriptSource.EMAIL:
        return "Email"
    return "Transcript"


def _current_phase(step: str) -> str:
    if step.startswith(_SUMMARY_STEPS):
        return "summary"
    if step.startswith(_INDEX_STEPS):
        return "index"
    if step.startswith(_SPEAKER_STEPS):
        return "speakers"
    return "transcript"


def _summary_detail(step: str) -> tuple[str, float | None]:
    if m := _CHUNK.search(step):
        idx, total = int(m.group(1)), int(m.group(2))
        # The merge is one more LLM call after the parts.
        return f"Part {idx} of {total}", (idx - 1) / (total + 1)
    if m := _MERGING.search(step):
        total = int(m.group(1))
        return "Merging parts", total / (total + 1)
    return "Writing summary", None


def _transcript_detail(step: str) -> tuple[str, float | None, str | None]:
    if m := _TRANSCRIBING.search(step):
        if m.group("tot"):
            return (
                f"{m.group('cur')} / {m.group('tot')}",
                int(m.group("pct")) / 100,
                m.group("eta"),
            )
        return m.group("cur"), None, None
    if step.startswith("sending audio"):
        if m := _PART.search(step):
            idx, total = int(m.group(1)), int(m.group(2))
            return f"Part {idx} of {total}", (idx - 1) / total, None
        return "Uploading audio", None, None
    if "downloading audio" in step:
        return "Downloading audio", None, None
    if "checking subtitles" in step or step == "fetching transcript":
        return "Checking subtitles", None, None
    if "subtitles found" in step:
        return "Subtitles loaded", 1.0, None
    if "reused" in step:
        return "Reused from another profile", 1.0, None
    if "fetching article" in step:
        return "Fetching article", None, None
    return step or "Starting", None, None


def describe(job: Job, video: Video, now: datetime | None = None) -> JobProgress:
    """Progress data for a running job. `now` is naive UTC, like the
    timestamps SQLite writes."""
    step = (job.step or "").strip()
    current = _current_phase(step)
    transcript_label = _transcript_label(step, video)

    keys = ["transcript", "summary", "index", "speakers"]
    labels = {
        "transcript": transcript_label,
        "summary": "Summary",
        "index": "Linking",
        "speakers": "Speakers",
    }
    pos = keys.index(current)
    phases = [
        Phase(k, labels[k], "done" if i < pos else "now" if i == pos else "todo")
        for i, k in enumerate(keys)
    ]

    eta = None
    if current == "transcript":
        headline, fraction, eta = _transcript_detail(step)
    elif current == "summary":
        headline, fraction = _summary_detail(step)
    elif current == "index":
        headline, fraction = "Finding related summaries", None
    else:
        headline, fraction = "Identifying speakers", None

    elapsed_s = None
    if job.started_at is not None:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        elapsed_s = max(0, int((now - job.started_at).total_seconds()))

    return JobProgress(
        phases=phases,
        label=labels[current],
        headline=headline,
        fraction=fraction,
        eta=eta,
        elapsed_s=elapsed_s,
    )
