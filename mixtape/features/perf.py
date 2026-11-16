"""The `perf` feature: measure a running pipeline with GStreamer's own tracers, for the live
page (`perf.summary`) and for CI (the artifacts of `out`).

Python never runs on a streaming thread for it: the core `latency` and `stats` tracers write
their records to the debug log file, which a thread tails, and the gst-plugins-rs tracers keep
theirs until a thread asks them to write them to a file (`write-log`)."""

import asyncio
import atexit
import contextlib
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst
from pydantic import PositiveFloat

from ..exceptions import FeatureError
from ..model import prop
from ..perf import RUST, Artifacts, Sample, Window, rates, window
from ..perf.reader import LogTail
from . import Feature
from .tracers import _entries, _name, active_tracers, add_log, add_tracers, installed, require


if TYPE_CHECKING:
    from ..player import Player

logger = logging.getLogger(__name__)

CORE = ("latency(flags=element+pipeline)", "stats")
"""The core tracers it runs: what they measure goes to the `GST_TRACER` debug log only"""

_init_ns: int | None = None
"""`time.monotonic_ns()` as GStreamer initialised, the start of its tracers' clock (that
clock is `gst_util_get_timestamp`, the monotonic clock Python reads too), from `_calibrate`"""
_owned: Path | None = None  # the GST_DEBUG_FILE `prepare` chose, whose other lines it echoes
_folder: Path | None = None  # for the files of this process, removed at exit
_tails: dict[Path, LogTail] = {}  # one per debug log file, for the life of the process


def _temp() -> Path:
    global _folder
    if _folder is None:
        _folder = Path(tempfile.mkdtemp(prefix="mixtape-perf-"))
        atexit.register(_cleanup)
    return _folder


def _cleanup() -> None:
    for tail in _tails.values():
        with contextlib.suppress(Exception):
            tail.close()  # echoes the last lines
    if _folder is not None:
        shutil.rmtree(_folder, ignore_errors=True)


def _calibrate(tail: LogTail) -> int | None:
    """`time.monotonic_ns()` as GStreamer initialised, which it does not tell: from the ts of
    the `new-element` record of an element made for it, None if the log has no record."""
    name = f"mixtape-perf-clock-{os.getpid()}"
    before = time.monotonic_ns()
    Gst.ElementFactory.make("identity", name)
    after = time.monotonic_ns()
    tail.catch_up()  # GStreamer wrote the record as it made the element
    if (ts := tail.parser.created.get(name)) is None:
        return None
    return (before + after) // 2 - ts


def _now() -> int:
    """Nanoseconds since GStreamer initialised."""
    assert _init_ns is not None
    return time.monotonic_ns() - _init_ns


def _keys(player: "Player") -> dict[str, str]:
    """The key of each element whose name the tracers report but which the player knows by
    another key: elements of the same name in different bins (`a.q`, `b.q`) keep their name,
    which does not say which of them it is."""
    keys: dict[str, str | None] = {}
    for key, element in player._element_keys().items():
        name = element.get_name()
        keys[name] = None if name in keys else key
    return {name: key for name, key in keys.items() if key is not None and key != name}


def _subject(subject: str, keys: dict[str, str]) -> str:
    """`subject` (`element`, `element:pad` or `src→sink`) with element keys for names."""
    if "→" in subject:
        return "→".join(_subject(part, keys) for part in subject.split("→"))
    element, colon, pad = subject.partition(":")
    return keys.get(element, element) + colon + pad


class Perf(Feature):
    """Measure the pipeline with GStreamer's own tracers: the core `latency` (elements and
    whole pipeline) and `stats` (buffers per pad), and the gst-plugins-rs `queue-levels`,
    `buffer-lateness` and `pad-push-timings` when they are installed. Their samples, and the
    CPU of the process, make a window every `interval` seconds, published as `perf.summary`.
    With `out`, a folder gets every sample, every window and the statistics of the whole run
    (see `mixtape.perf.Artifacts`).

    The core tracers write to the debug log only: it adds `GST_TRACER:7` to `GST_DEBUG` and
    reads `GST_DEBUG_FILE`, which it sets to a file of its own unless the environment has
    one; then it echoes the lines that are not the tracers' to stderr, as GStreamer would.

    Tracers are created by `Gst.init` and never later, one of each per process: build the
    player with `Player.from_description` or `mixtape run`, first in the process. One player
    at a time can measure, as they share the tracers.
    """

    name = "perf"

    def __init__(self, out: Path | None = None, interval: PositiveFloat = 1.0) -> None:
        self.out = out
        """The folder of the artifacts, or None for none"""
        self.interval = interval
        """Seconds per window"""
        self._log: Path | None = None
        self._rust: dict[str, Any] = {}  # the tracers of gst-plugins-rs it reads, by name
        self._lock = threading.Lock()  # one collection at a time: the poll's or the last
        self._tail: LogTail | None = None
        self._artifacts: Artifacts | None = None
        self._start = self._first = 0  # ns: of the window collecting, of the run
        self._cpu = (0.0, 0.0)  # process time and monotonic time at the window's start
        self._summary: Window | None = None
        self._graph: dict[str, Any] | None = None  # for the report: the latest snapshot
        self._task: asyncio.Task[None] | None = None

    def prepare(self) -> None:
        global _owned
        for asked, kept in add_tracers([*CORE, *RUST]).items():
            logger.warning("GST_TRACERS already has %r: keeping it rather than %r", kept, asked)
        if (debug := add_log()) is not None:
            logger.warning("GST_DEBUG already has %r: keeping it", debug)
        os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")
        if not os.environ.get("GST_DEBUG_FILE"):
            _owned = _temp() / "gst-debug.log"
            os.environ["GST_DEBUG_FILE"] = str(_owned)
        # GStreamer puts the process id for a `%p`
        self._log = Path(os.environ["GST_DEBUG_FILE"].replace("%p", str(os.getpid())))

    def check(self, player: "Player") -> None:
        require(self.name, map(_name, CORE), self.name)
        rust = installed(RUST)
        if missing := [name for name in RUST if name not in rust]:
            logger.warning(
                "GStreamer has no tracer %s: perf goes on without what it measures; the "
                "rstracers plugin of gst-plugins-rs has them",
                ", ".join(map(repr, missing)),
            )
        require(self.name, rust, self.name)
        if self._log is None or not self._log.is_file():
            raise FeatureError(
                f"The feature 'perf' cannot read the debug log {str(self._log)!r}: GStreamer "
                "was initialised before the feature could set GST_DEBUG_FILE, or the file "
                "is gone"
            )
        if (tail := _tails.get(self._log)) is None:
            tail = _tails[self._log] = LogTail(
                self._log, sys.stderr if self._log == _owned else None
            )
        self._tail = tail
        global _init_ns
        if _init_ns is None and (_init_ns := _calibrate(tail)) is None:
            raise FeatureError(
                "The feature 'perf' finds no tracer records in the debug log "
                f"{str(self._log)!r}: GST_DEBUG has to log GST_TRACER at level 7"
            )
        active = active_tracers()
        self._rust = {name: active[name] for name in rust}

    def attach(self, player: "Player") -> None:
        assert self._tail is not None  # `check` ran
        self._flush_rust()  # what they kept before is not of this run
        self._tail.collect(True)
        self._start = self._first = _now()
        self._cpu = (time.process_time(), time.monotonic())
        self._artifacts = Artifacts(self.out) if self.out is not None else None
        self._snapshot(player)
        self._task = asyncio.get_running_loop().create_task(self._poll(player))

    def detach(self, player: "Player") -> None:
        if self._task is not None and not self._task.get_loop().is_closed():
            self._task.cancel()  # a closed loop cannot cancel: the task died with it
        self._task = None
        self._summary = None
        try:  # what the run did since the last window, and its end
            self._collect(self._cpu_sample(), _keys(player))
        finally:
            assert self._tail is not None
            self._tail.collect(False)
            if self._artifacts is not None:
                used = {*map(_name, CORE), *self._rust}
                entries = _entries(os.environ.get("GST_TRACERS", ""))
                self._artifacts.finish(
                    {
                        "pipeline": player._description,
                        "tracers": [entry for entry in entries if _name(entry) in used],
                        "interval": self.interval,
                        "start_ns": self._first,
                        "end_ns": self._start,
                        "graph": self._graph,
                    }
                )
                self._graph = None
                self._artifacts = None

    async def _poll(self, player: "Player") -> None:
        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            due += self.interval
            await asyncio.sleep(max(due - loop.time(), 0))
            cpu, keys = self._cpu_sample(), _keys(player)
            self._snapshot(player)
            self._summary = await asyncio.to_thread(self._collect, cpu, keys)
            self.notify("summary")

    def _snapshot(self, player: "Player") -> None:
        """Keep the pipeline's graph for the report of `out`, as of now: each window takes one,
        as elements come with the stream (a decodebin's) and caps with negotiation, and both
        are gone by `detach`, in NULL."""
        if self._artifacts is None:
            return
        try:
            self._graph = player.graph().model_dump()
        except Exception:
            logger.exception("perf could not snapshot the pipeline's graph for its report")

    def _cpu_sample(self) -> Sample | None:
        """`process.cpu` since the last call, taken on the loop."""
        cpu, wall = time.process_time(), time.monotonic()
        (last_cpu, last_wall), self._cpu = self._cpu, (cpu, wall)
        if wall <= last_wall:
            return None
        return Sample(_now(), "process.cpu", "process", (cpu - last_cpu) / (wall - last_wall) * 100)

    def _collect(self, cpu: Sample | None, keys: dict[str, str]) -> Window:
        """Close the window collecting, with the subjects named by `keys` (see `_keys`):
        off the loop, as it reads and writes files."""
        assert self._tail is not None
        with self._lock:
            samples = self._flush_rust()
            logged, counts = self._tail.take()
            samples += logged
            end = _now()
            samples += rates(counts, self._start, end)
            if keys:
                samples = [s._replace(subject=_subject(s.subject, keys)) for s in samples]
            if cpu is not None:
                samples.append(cpu)
            result = window(samples, self._start, end)
            if self._artifacts is not None:
                self._artifacts.add(samples, result)
            self._start = end
            return result

    def _flush_rust(self) -> list[Sample]:
        """What the gst-plugins-rs tracers measured since the last call: `write-log` writes
        it to a file, and forgets it."""
        samples: list[Sample] = []
        for name, tracer in self._rust.items():
            path = _temp() / f"{name}.csv"
            tracer.emit("write-log", str(path))  # appends, after the first time
            with open(path, encoding="utf-8", errors="replace") as file:
                lines = file.readlines()
            os.truncate(path, 0)
            samples += RUST[name](lines)
        return samples

    @prop
    def summary(self) -> Window | None:
        """The statistics of the last window, by metric and subject, while attached"""
        return self._summary
