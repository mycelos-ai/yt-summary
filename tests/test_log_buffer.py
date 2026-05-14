import logging

from app.services.log_buffer import RingBufferHandler


def _record(msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )


def test_emit_appends_formatted_line():
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.emit(_record("hello"))
    assert h.snapshot() == ["INFO hello"]


def test_capacity_caps_buffer_length():
    h = RingBufferHandler(capacity=3)
    h.setFormatter(logging.Formatter("%(message)s"))
    for i in range(5):
        h.emit(_record(f"line{i}"))
    # Oldest two dropped.
    assert h.snapshot() == ["line2", "line3", "line4"]


def test_snapshot_returns_copy_that_survives_further_writes():
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("first"))
    snap = h.snapshot()
    h.emit(_record("second"))
    # The earlier snapshot must not see "second".
    assert snap == ["first"]


def test_snapshot_limit_slices_tail():
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    for i in range(6):
        h.emit(_record(f"l{i}"))
    assert h.snapshot(limit=2) == ["l4", "l5"]


def test_snapshot_limit_zero_returns_empty():
    """Edge case: -0 == 0 in Python, so a naive ``data[-limit:]``
    slice with limit=0 returns the full list. Guard against that."""
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("x"))
    assert h.snapshot(limit=0) == []


def test_snapshot_negative_limit_returns_empty():
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("x"))
    assert h.snapshot(limit=-5) == []


def test_emit_swallows_formatter_exceptions():
    """A bad formatter must not crash the logger pipeline."""
    h = RingBufferHandler(capacity=10)
    # %(nonexistent_field) raises KeyError inside Formatter.format.
    h.setFormatter(logging.Formatter("%(nonexistent_field)s"))
    h.emit(_record("ignored"))
    # No exception → snapshot is empty (the bad line was dropped).
    assert h.snapshot() == []


def test_works_attached_to_root_logger():
    """Smoke test: attach to a logger and emit through .info()."""
    logger = logging.getLogger("yt_summary.test.logbuffer")
    logger.setLevel(logging.INFO)
    h = RingBufferHandler(capacity=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
    try:
        logger.info("via-logger")
        assert "via-logger" in h.snapshot()
    finally:
        logger.removeHandler(h)
