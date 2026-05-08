#!/usr/bin/env python3
"""Analyze a music track for video-editing-relevant cues.

Reads an audio file and emits JSON describing:

  - tempo (bpm)
  - beat timestamps (seconds)
  - downbeat candidates (every Nth beat, default 4)
  - low-energy windows (relative quiet stretches — good for cut points)
  - rough section boundaries (where the energy profile changes shape)

The output is meant to drive Remotion sequence timing. Cuts placed at
section boundaries that fall inside a low-energy window AND on a
downbeat will feel intentional rather than random.

Usage:
    python scripts/analyze_music.py promos/public/music/promo_2.mp3
    python scripts/analyze_music.py promos/public/music/promo_2.mp3 -o cuts.json
    python scripts/analyze_music.py promos/public/music/promo_2.mp3 --plot

Independent of the yt-summary app. Lives under scripts/ alongside
generate_music.py — same "tools, not service" bucket.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import librosa
import numpy as np


def analyze(
    audio_path: Path,
    *,
    beats_per_bar: int = 4,
    quiet_threshold_db: float = -6.0,
    quiet_min_duration_s: float = 0.5,
    section_min_gap_s: float = 4.0,
) -> dict:
    """Return the cue dict for `audio_path`.

    quiet_threshold_db: how far below the median RMS counts as "quiet".
        -6 dB ≈ half the loudness of the median. Lower (more negative)
        → only really quiet stretches qualify. Default -6.

    quiet_min_duration_s: ignore tiny dips. Default 0.5s.

    section_min_gap_s: don't report two section boundaries closer than
        this. Default 4s.
    """
    print(f"  loading {audio_path.name} …", file=sys.stderr)
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))

    # ── Tempo + beats ─────────────────────────────────────────
    print("  tracking tempo + beats …", file=sys.stderr)
    tempo_arr, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    # Newer librosa returns ndarray for tempo; coerce to plain float.
    tempo = float(np.atleast_1d(tempo_arr)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()

    # ── Downbeats: every beats_per_bar-th beat, starting at the first.
    # Real downbeat detection (madmom etc.) is a bigger dep — for
    # video cuts on synthwave the every-Nth approximation is fine.
    downbeat_times = beat_times[::beats_per_bar]

    # ── RMS energy / low-energy windows ───────────────────────
    print("  computing RMS energy …", file=sys.stderr)
    hop_length = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop_length)[0]
    rms_times = librosa.frames_to_time(
        np.arange(len(rms)), sr=sr, hop_length=hop_length
    )
    rms_db = librosa.amplitude_to_db(rms, ref=np.median)

    low_windows = _find_quiet_windows(
        rms_db,
        rms_times,
        threshold_db=quiet_threshold_db,
        min_duration_s=quiet_min_duration_s,
    )

    # ── Section boundaries ────────────────────────────────────
    # Spectral self-similarity → novelty curve → peak picks.
    print("  finding section boundaries …", file=sys.stderr)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)
    boundary_frames = librosa.segment.agglomerative(chroma, k=8)
    boundary_times = librosa.frames_to_time(
        boundary_frames, sr=sr, hop_length=hop_length
    ).tolist()
    # Drop duplicates within section_min_gap_s of each other.
    deduped: list[float] = []
    for t in boundary_times:
        if not deduped or t - deduped[-1] >= section_min_gap_s:
            deduped.append(round(float(t), 3))
    section_boundaries = deduped

    return {
        "file": str(audio_path),
        "duration_s": round(duration, 3),
        "tempo_bpm": round(tempo, 2),
        "beats_per_bar": beats_per_bar,
        "beats_s": [round(t, 3) for t in beat_times],
        "downbeats_s": [round(t, 3) for t in downbeat_times],
        "low_energy_windows": [
            {
                "start": round(w["start"], 3),
                "end": round(w["end"], 3),
                "duration": round(w["end"] - w["start"], 3),
                "rms_db": round(w["rms_db"], 2),
            }
            for w in low_windows
        ],
        "section_boundaries_s": section_boundaries,
    }


def _find_quiet_windows(
    rms_db: np.ndarray,
    times: np.ndarray,
    *,
    threshold_db: float,
    min_duration_s: float,
) -> list[dict]:
    """Group contiguous frames where rms_db < threshold_db into windows.

    Returns list of {start, end, rms_db (mean of the window)}.
    """
    below = rms_db < threshold_db
    out: list[dict] = []
    if len(below) == 0:
        return out
    in_window = False
    start_idx = 0
    for i, q in enumerate(below):
        if q and not in_window:
            in_window = True
            start_idx = i
        elif not q and in_window:
            in_window = False
            _maybe_emit(
                out, rms_db, times, start_idx, i, min_duration_s
            )
    if in_window:
        _maybe_emit(
            out, rms_db, times, start_idx, len(below), min_duration_s
        )
    return out


def _maybe_emit(
    out: list[dict],
    rms_db: np.ndarray,
    times: np.ndarray,
    start_idx: int,
    end_idx: int,
    min_duration_s: float,
) -> None:
    start = float(times[start_idx])
    end = float(times[min(end_idx, len(times) - 1)])
    if end - start < min_duration_s:
        return
    mean_db = float(np.mean(rms_db[start_idx:end_idx]))
    out.append({"start": start, "end": end, "rms_db": mean_db})


def snap_to_downbeat(
    target_time_s: float, downbeats_s: list[float]
) -> float:
    """Helper for callers: snap any time to the nearest downbeat.

    Used by Remotion-side TS code via the JSON output, but exposed
    here too in case a Python helper wants it.
    """
    if not downbeats_s:
        return target_time_s
    idx = int(np.argmin([abs(target_time_s - d) for d in downbeats_s]))
    return downbeats_s[idx]


def maybe_plot(cues: dict, audio_path: Path) -> None:
    """Render an optional debug PNG showing waveform + beats + low
    windows + section boundaries. Useful for sanity-checking the
    analysis output by eye."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("✗ --plot requires matplotlib. pip install matplotlib")

    print("  rendering plot …", file=sys.stderr)
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    fig, ax = plt.subplots(figsize=(14, 4))
    librosa.display.waveshow(y, sr=sr, ax=ax, alpha=0.35)
    for w in cues["low_energy_windows"]:
        ax.axvspan(w["start"], w["end"], color="orange", alpha=0.25)
    for b in cues["section_boundaries_s"]:
        ax.axvline(b, color="green", lw=2, alpha=0.7)
    for d in cues["downbeats_s"]:
        ax.axvline(d, color="red", lw=0.6, alpha=0.4)
    ax.set_title(
        f"{audio_path.name}  ·  "
        f"{cues['tempo_bpm']:.1f} BPM  ·  "
        f"{cues['duration_s']:.1f}s  ·  "
        f"red=downbeats  green=sections  orange=quiet windows"
    )
    ax.set_xlabel("seconds")
    plt.tight_layout()
    plot_path = audio_path.with_suffix(".analysis.png")
    fig.savefig(plot_path, dpi=120)
    plt.close(fig)
    print(f"  ✓ wrote {plot_path}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(
        prog="analyze_music.py",
        description=(
            "Extract tempo, beats, downbeats, low-energy windows, and "
            "section boundaries from an audio file. Emits JSON."
        ),
    )
    p.add_argument("audio", type=Path, help="Audio file to analyze")
    p.add_argument(
        "-o", "--output", type=Path,
        help="Where to write the JSON. Default: <audio>.analysis.json",
    )
    p.add_argument(
        "--beats-per-bar", type=int, default=4,
        help="Used to derive downbeats. Default 4.",
    )
    p.add_argument(
        "--quiet-threshold-db", type=float, default=-6.0,
        help="dB below median RMS that counts as quiet. Default -6.",
    )
    p.add_argument(
        "--plot", action="store_true",
        help="Also write <audio>.analysis.png with a debug visualization.",
    )
    args = p.parse_args()

    if not args.audio.exists():
        sys.exit(f"✗ Not found: {args.audio}")

    cues = analyze(
        args.audio,
        beats_per_bar=args.beats_per_bar,
        quiet_threshold_db=args.quiet_threshold_db,
    )

    out_path = args.output or args.audio.with_suffix(".analysis.json")
    out_path.write_text(json.dumps(cues, indent=2))
    print(f"\n✓ wrote {out_path}")
    print(
        f"  tempo: {cues['tempo_bpm']} BPM"
        f" · beats: {len(cues['beats_s'])}"
        f" · downbeats: {len(cues['downbeats_s'])}"
        f" · quiet windows: {len(cues['low_energy_windows'])}"
        f" · sections: {len(cues['section_boundaries_s'])}"
    )

    if args.plot:
        maybe_plot(cues, args.audio)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n^C — aborted by user.")
        sys.exit(130)
