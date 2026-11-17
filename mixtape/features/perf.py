"""The `perf` feature: measure a running pipeline with GStreamer's own tracers, for the live
page (`perf.summary`) and for CI (the artifacts of `out`).

Python never runs on a streaming thread for it: the `Collector` of `mixtape.perf.collect` reads
the records of the core `latency` and `stats` tracers from GStreamer's ring buffer logger, and
asks the gst-plugins-rs tracers to write theirs to a file (`write-log`)."""

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import PositiveFloat

from ..model import prop
from ..perf import Artifacts, Sample, Window, rates, window
from ..perf.collect import Collector, prepare
from . import Feature
from .tracers import _entries, _name


if TYPE_CHECKING:
    from ..player import Player

logger = logging.getLogger(__name__)


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

    The core tracers write to the debug log only: it turns on its `GST_TRACER` category at level
    7 (unless `GST_DEBUG` names it) and reads the log from GStreamer's ring buffer logger, in
    memory, instead of its default logger, and echoes the lines that are not the tracers' to
    stderr as that would. With a `GST_DEBUG_FILE` in the environment the default logger stays,
    writing there, and it echoes nothing.

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
        self._lock = threading.Lock()  # one collection at a time: the poll's or the last
        self._collector: Collector | None = None
        self._artifacts: Artifacts | None = None
        self._start = self._first = 0  # ns: of the window collecting, of the run
        self._cpu = (0.0, 0.0)  # process time and monotonic time at the window's start
        self._summary: Window | None = None
        self._graph: dict[str, Any] | None = None  # for the report: the latest snapshot
        self._snapshot_failed = False
        self._task: asyncio.Task[None] | None = None

    def prepare(self) -> None:
        prepare()

    def initialised(self) -> None:
        Collector.get()  # before the pipeline's elements: their names are in the log

    def check(self, player: "Player") -> None:
        self._collector = Collector.get()

    def attach(self, player: "Player") -> None:
        assert self._collector is not None  # `check` ran
        self._collector.start()
        self._start = self._first = self._collector.now()
        self._cpu = (time.process_time(), time.monotonic())
        self._artifacts = Artifacts(self.out) if self.out is not None else None
        self._snapshot_failed = False
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
            assert self._collector is not None
            self._collector.stop()
            if self._artifacts is not None:
                used = set(self._collector.tracers)
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
            if not self._snapshot_failed:  # once a run, not every window
                logger.exception("perf could not snapshot the pipeline's graph for its report")
            self._snapshot_failed = True

    def _cpu_sample(self) -> Sample | None:
        """`process.cpu` since the last call, taken on the loop."""
        cpu, wall = time.process_time(), time.monotonic()
        (last_cpu, last_wall), self._cpu = self._cpu, (cpu, wall)
        if wall <= last_wall:
            return None
        assert self._collector is not None
        percent = (cpu - last_cpu) / (wall - last_wall) * 100
        return Sample(self._collector.now(), "process.cpu", "process", percent)

    def _collect(self, cpu: Sample | None, keys: dict[str, str]) -> Window:
        """Close the window collecting, with the subjects named by `keys` (see `_keys`):
        off the loop, as it reads and writes files."""
        assert self._collector is not None
        with self._lock:
            samples, counts = self._collector.take()
            end = self._collector.now()
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

    @prop
    def summary(self) -> Window | None:
        """The statistics of the last window, by metric and subject, while attached"""
        return self._summary
