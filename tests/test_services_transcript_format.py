"""Tests for the (start, text) → grouped paragraph helper."""

from app.services.transcript_format import format_timestamp, group_segments


def test_group_segments_empty_returns_empty():
    assert group_segments([]) == []


def test_group_segments_single_cue_one_block():
    out = group_segments([(1.5, "hello")], gap_s=8.0)
    assert out == [{"start": 1.5, "text": "hello"}]


def test_group_segments_consecutive_cues_collapse_into_one_block():
    """Two cues 4s apart land in the same paragraph (gap < 8s)."""
    out = group_segments(
        [(0.0, "Hello"), (4.0, "and welcome.")],
        gap_s=8.0,
    )
    assert out == [{"start": 0.0, "text": "Hello and welcome."}]


def test_group_segments_large_gap_starts_new_block():
    """A gap of 12s exceeds the 8s threshold → new paragraph."""
    out = group_segments(
        [(0.0, "First topic."), (12.0, "Second topic.")],
        gap_s=8.0,
    )
    assert out == [
        {"start": 0.0, "text": "First topic."},
        {"start": 12.0, "text": "Second topic."},
    ]


def test_group_segments_long_run_with_pauses():
    """A natural transcript: a few cues close together, then a pause,
    then a few more — should produce two paragraphs."""
    cues = [
        (0.0, "A"),
        (3.0, "B"),
        (5.5, "C"),
        # gap of 10s
        (15.5, "D"),
        (18.0, "E"),
    ]
    out = group_segments(cues, gap_s=8.0)
    assert out == [
        {"start": 0.0, "text": "A B C"},
        {"start": 15.5, "text": "D E"},
    ]


def test_group_segments_custom_gap_threshold():
    """A 4s gap with default 8s threshold = same block; with a 3s
    threshold it would start a new one."""
    cues = [(0.0, "X"), (4.0, "Y")]
    assert group_segments(cues, gap_s=8.0) == [
        {"start": 0.0, "text": "X Y"},
    ]
    assert group_segments(cues, gap_s=3.0) == [
        {"start": 0.0, "text": "X"},
        {"start": 4.0, "text": "Y"},
    ]


def test_format_timestamp_short_video():
    assert format_timestamp(0.0) == "00:00"
    assert format_timestamp(5.0) == "00:05"
    assert format_timestamp(65.7) == "01:05"
    assert format_timestamp(599.0) == "09:59"


def test_format_timestamp_long_video_uses_hours():
    assert format_timestamp(3600.0) == "1:00:00"
    assert format_timestamp(3725.0) == "1:02:05"


def test_format_timestamp_total_duration_promotes_to_hours():
    """A 5-second mark in a 2-hour video should still render as
    H:MM:SS so all timestamps in the transcript are uniform."""
    assert format_timestamp(5.0, total_duration_s=7200.0) == "0:00:05"
    assert format_timestamp(125.0, total_duration_s=7200.0) == "0:02:05"
