"""A thread that tails GStreamer's debug log file: it parses the tracer records and echoes the
other lines, so Python never runs on a streaming thread to get them."""

import os
import threading
from pathlib import Path
from typing import TextIO

from .metrics import Sample
from .parse import TracerLog

POLL = 0.05
"""Seconds the thread waits at the end of the file before it reads again"""


class LogTail:
    """Tail the debug log at `path` from its start on a daemon thread, feeding `parser`
    the lines of the `GST_TRACER` category and writing the others to `echo`, if any.

    GStreamer writes the file line by line (and flushes each) from its streaming threads;
    `take` reads up to its end first, so it has every record written before it was called."""

    def __init__(self, path: Path, echo: TextIO | None) -> None:
        self.path = path
        self.echo = echo
        self.parser = TracerLog()
        self.lines = 0
        """Lines read, tracer records or not"""
        self._lock = threading.Lock()  # the parser and the file
        self._file = open(path, "rb")  # for the life of the thread
        self._rest = b""  # the start of a line GStreamer has not finished writing
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mixtape-perf-log", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                read = self._read()
            if not read:
                self._stop.wait(POLL)

    def _read(self) -> bool:
        """Read and handle what was written since the last read; whether there was some."""
        chunk = self._file.read(1 << 20)
        if not chunk:
            return False
        *lines, self._rest = (self._rest + chunk).split(b"\n")
        self.lines += len(lines)
        echoed: list[str] = []
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            if not self.parser.line(line) and self.echo is not None:
                echoed.append(line + "\n")
        if echoed and self.echo is not None:
            self.echo.writelines(echoed)
            self.echo.flush()
        return True

    def _drain(self) -> None:
        """Read up to where the file ends now, under the lock."""
        end = os.fstat(self._file.fileno()).st_size
        while self._file.tell() < end and self._read():
            pass

    def catch_up(self) -> None:
        """Read up to where the file ends now."""
        with self._lock:
            self._drain()

    def collect(self, on: bool) -> None:
        """Start keeping samples from now on (forgetting those before), or stop."""
        with self._lock:
            self._drain()
            self.parser.collecting = on
            self.parser.reset()

    def take(self) -> tuple[list[Sample], dict[str, tuple[int, int]]]:
        """Read up to where the file ends now, then the parser's samples and counts since the
        last call (see `TracerLog.take`)."""
        with self._lock:
            self._drain()
            return self.parser.take()

    def close(self) -> None:
        """Read what is left, then stop the thread and close the file."""
        self.catch_up()
        self._stop.set()
        self._thread.join()
        self._file.close()
