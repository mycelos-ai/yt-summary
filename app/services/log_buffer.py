"""In-memory ring-buffer logging handler.

Installed once at app startup on the root logger. The diagnostics
page reads :meth:`snapshot` and renders it as a ``<pre>`` block so
the operator can see recent worker activity without shelling into
the container.

Lifetime is process-local — a restart wipes it. That's fine: the
operator only ever cares about *this* process's logs.
"""
from __future__ import annotations

import logging
import threading
from collections import deque


class RingBufferHandler(logging.Handler):
    """Thread-safe ring buffer of formatted log lines.

    :meth:`emit` is called by the logging framework (possibly from
    worker threads, e.g. ``faster-whisper`` or Piper synthesis); it
    appends to a bounded :class:`collections.deque`. :meth:`snapshot`
    takes a lock and copies the deque so the caller can iterate
    without racing further writes.

    Capacity defaults to 500 lines (~30–80 KB at typical log line
    lengths). The diagnostics view shows the last 200 by default.
    """

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buf: deque[str] = deque(maxlen=capacity)
        # collections.deque append/popleft are thread-safe in CPython,
        # but iterating *while another thread appends* is not. The
        # lock protects only the snapshot read; writes stay lock-free.
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        # Python's logging contract: a handler must never raise. We
        # mirror the stdlib pattern (bpo-36272) — re-raise
        # RecursionError so a broken call stack stays visible, but
        # drop any other formatter error silently.
        try:
            line = self.format(record)
        except RecursionError:
            raise
        except Exception:
            return
        self._buf.append(line)

    def snapshot(self, limit: int | None = None) -> list[str]:
        """Return the buffered lines, optionally trimmed to the last
        ``limit`` entries."""
        with self._lock:
            data = list(self._buf)
        if limit is not None:
            if limit <= 0:
                return []
            if len(data) > limit:
                return data[-limit:]
        return data
