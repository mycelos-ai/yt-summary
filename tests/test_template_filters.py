from datetime import datetime, timedelta

from app.template_filters import format_duration, relative_time


def test_relative_time_just_now():
    now = datetime.now()
    assert relative_time(now - timedelta(seconds=5), now=now) == "just now"


def test_relative_time_minutes_ago():
    now = datetime.now()
    assert relative_time(now - timedelta(minutes=46), now=now) == "46 minutes ago"


def test_relative_time_one_minute_singular():
    now = datetime.now()
    assert relative_time(now - timedelta(minutes=1), now=now) == "1 minute ago"


def test_relative_time_hours_ago_same_day():
    # 3 hours ago, but still same calendar day
    now = datetime(2026, 5, 6, 18, 0, 0)
    earlier = datetime(2026, 5, 6, 15, 0, 0)
    assert relative_time(earlier, now=now) == "3 hours ago"


def test_relative_time_yesterday_falls_back_to_date():
    now = datetime(2026, 5, 6, 8, 0, 0)
    yesterday = datetime(2026, 5, 5, 22, 0, 0)
    assert relative_time(yesterday, now=now) == "2026-05-05"


def test_relative_time_none_returns_empty_string():
    assert relative_time(None) == ""


def test_format_duration_under_one_hour():
    assert format_duration(754) == "12:34"


def test_format_duration_pads_seconds():
    assert format_duration(65) == "1:05"


def test_format_duration_with_hours():
    # 1h 23m 45s
    assert format_duration(3600 + 23 * 60 + 45) == "1:23:45"


def test_format_duration_none_or_zero_returns_empty_string():
    assert format_duration(None) == ""
    assert format_duration(0) == ""
