from datetime import datetime

from app.services.heartbeat import Heartbeat, HeartbeatRegistry


def test_touch_records_heartbeat_for_new_worker():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_job_id=42, current_step="downloading")
    snap = reg.snapshot()
    assert "summary_worker" in snap
    hb = snap["summary_worker"]
    assert isinstance(hb, Heartbeat)
    assert hb.name == "summary_worker"
    assert hb.current_job_id == 42
    assert hb.current_step == "downloading"
    assert isinstance(hb.last_tick_at, datetime)
    assert hb.last_tick_at.tzinfo is None  # UTC-naive — matches SQLite's datetime('now')


def test_touch_updates_existing_worker():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_job_id=1, current_step="a")
    reg.touch("summary_worker", current_job_id=2, current_step="b")
    snap = reg.snapshot()
    assert snap["summary_worker"].current_job_id == 2
    assert snap["summary_worker"].current_step == "b"


def test_touch_with_no_job_marks_worker_idle():
    reg = HeartbeatRegistry()
    reg.touch("tts_worker")
    snap = reg.snapshot()
    assert snap["tts_worker"].current_job_id is None
    assert snap["tts_worker"].current_step is None


def test_multiple_workers_do_not_clobber_each_other():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_job_id=1, current_step="x")
    reg.touch("tts_worker", current_job_id=99, current_step="rendering")
    reg.touch("scheduler", current_step="p1")
    snap = reg.snapshot()
    assert snap["summary_worker"].current_job_id == 1
    assert snap["tts_worker"].current_step == "rendering"
    assert snap["scheduler"].current_step == "p1"


def test_snapshot_returns_a_copy():
    reg = HeartbeatRegistry()
    reg.touch("summary_worker", current_step="initial")
    snap = reg.snapshot()
    reg.touch("summary_worker", current_step="later")
    # The earlier snapshot must not have changed.
    assert snap["summary_worker"].current_step == "initial"
