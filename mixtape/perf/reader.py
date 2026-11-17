"""A thread that polls GStreamer's ring-buffer logger: it parses the tracer records and echoes
the other lines, so Python never runs on a streaming thread to get them.

The ring buffer logger keeps the last bytes each thread logged in memory (the default logger
and a file would write every line out, a gigabyte an hour for a video pipeline) and gives the
whole ring on each call, so the tail keeps the last line it saw of each thread."""

import logging
import re
import threading
from collections.abc import Callable
from operator import itemgetter
from typing import TextIO

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .metrics import Sample
from .parse import TracerLog

logger = logging.getLogger(__name__)

RING = 1 << 20
"""Bytes the ring buffer logger keeps of each thread"""
TIMEOUT = 5
"""Seconds without a line after which the logger forgets the ring of a thread"""
POLL = 0.25
"""Seconds between two reads of the rings"""

_TIME = re.compile(r"\d+:\d\d:\d\d\.\d{9} ")  # a message starts with its time: `H:MM:SS.nnnnnnnnn`
_START = re.compile(_TIME.pattern, re.MULTILINE)  # at the start of a line


def _logs() -> list[str]:
    return list(Gst.debug_ring_buffer_logger_get_logs())


class RingTail:
    """Poll the ring buffer logger on a daemon thread, feeding `parser` the lines of the
    `GST_TRACER` category and writing the others to `echo`, if any.

    With `echo` it installs the logger and removes GStreamer's default one, which would
    write the same lines to stderr, so `echo` is where they go; without it the default logger
    stays (a `GST_DEBUG_FILE` the user chose). `get_logs` is where the rings come from, one
    string per thread: the logger's, unless given (the tests do), which then is not installed.

    GStreamer drops the oldest lines of a ring that is full: when the line a ring was read up to
    is gone from a ring nearly as big as `size`, the lines after it are lost, and `lost` counts
    it. `take` reads the rings first, so it has every record written before it was called."""

    def __init__(
        self,
        echo: TextIO | None,
        get_logs: Callable[[], list[str]] | None = None,
        size: int = RING,
        poll: float = POLL,
    ) -> None:
        self.echo = echo
        self.parser = TracerLog()
        self.size = size
        self.lost = 0
        """Times a ring dropped lines before they were read, since the last `collect(True)`"""
        self._get_logs = get_logs or _logs
        self._last: dict[str, str] = {}  # thread: the last line of its ring that was read
        self._warned = False
        self._failed = False
        self._lock = threading.Lock()  # the parser and `_last`
        if get_logs is None:
            Gst.debug_add_ring_buffer_logger(size, TIMEOUT)
            if echo is not None:
                Gst.debug_remove_log_function(None)
        self._interval = poll
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mixtape-perf-log", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.catch_up()
            except Exception:  # the thread has to outlive it: every `take` reads as well
                if not self._failed:  # once, not every poll
                    self._failed = True
                    logger.exception("perf could not read GStreamer's debug log")

    @staticmethod
    def _last_message(text: str) -> int:
        """Where the last message of a ring starts, which has a message at its start. A message
        is a line with its time and the lines after it that have none: the ring keeps the
        newlines of a message (`multi\nline\n` is stored as `…: multi\nline\n\n`)."""
        pos = len(text)
        while pos > 0:
            pos = text.rfind("\n", 0, pos - 1) + 1
            if _TIME.match(text, pos):
                return pos
        return 0

    def _read(self) -> None:
        """Handle the lines logged since the last read, oldest first."""
        fresh: list[tuple[tuple[int, str], str]] = []  # (time, line), of the rings together
        last: dict[str, str] = {}
        for text in self._get_logs():
            # a full ring starts anywhere in a message: what comes before the first one is not
            # of a thread we can tell
            if (first := _START.search(text)) is None:
                continue
            text = text[first.start() :]
            fields = text[:400].split(None, 3)
            thread = fields[2] if len(fields) > 2 else ""
            last[thread] = text[self._last_message(text) :]  # whole, and unique: it has its time
            seen = self._last.get(thread)
            if seen is not None and (at := text.rfind(seen)) >= 0:
                text = text[at + len(seen) :]
            elif seen is not None and len(text) >= self.size * 3 // 4:
                self._overflow()  # a ring far under its size is a new thread of the same name
            key = (0, "")
            for line in text.split("\n")[:-1]:
                if (stamp := _TIME.match(line)) is not None:  # one without continues the message
                    key = (stamp.end(), line[: stamp.end()])
                fresh.append((key, line))
        self._last = last  # a thread whose ring is gone is forgotten
        fresh.sort(key=itemgetter(0))
        echoed: list[str] = []
        for _, line in fresh:
            if not self.parser.line(line) and self.echo is not None:
                echoed.append(line + "\n")
        if echoed and self.echo is not None:
            self.echo.writelines(echoed)
            self.echo.flush()

    def _overflow(self) -> None:
        self.lost += 1
        if not self._warned:  # once a run, not every poll
            self._warned = True
            logger.warning(
                "perf lost debug log lines: GStreamer logged more than %d bytes of a thread "
                "between two reads, so some tracer records are missing",
                self.size,
            )

    def catch_up(self) -> None:
        """Read what is logged now."""
        with self._lock:
            self._read()

    def collect(self, on: bool) -> None:
        """Start keeping samples from now on (forgetting those before), or stop."""
        with self._lock:
            self._read()
            self.parser.collecting = on
            self.parser.reset()
            if on:
                self.lost, self._warned = 0, False

    def take(self) -> tuple[list[Sample], dict[str, tuple[int, int]]]:
        """Read what is logged now, then the parser's samples and counts since the last call
        (see `TracerLog.take`)."""
        with self._lock:
            self._read()
            return self.parser.take()

    def close(self) -> None:
        """Read what is left, then stop the thread."""
        self.catch_up()
        self._stop.set()
        self._thread.join()
