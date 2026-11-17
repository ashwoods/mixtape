"""The collector of what GStreamer's tracers measure, one per process: the core `latency` and
`stats` write their records to the debug log, which a thread polls (`RingTail`), and the
gst-plugins-rs ones keep theirs until a thread asks them to write them to a file (`write-log`).
Python never runs on a streaming thread for either.

Tracers are created by `Gst.init` and never later: `prepare` sets the environment before it,
`Collector.get` is for after it, and before the first element is made, as the log starts then."""

import atexit
import contextlib
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from ..exceptions import FeatureError
from ..features.tracers import (
    LOG,
    _entries,
    _name,
    active_tracers,
    add_tracers,
    installed,
    require,
)
from .metrics import Sample
from .parse import RUST
from .reader import RingTail

logger = logging.getLogger(__name__)

CORE = ("latency(flags=element+pipeline)", "stats")
"""The core tracers it runs: what they measure goes to the `GST_TRACER` debug log only"""


def prepare() -> None:
    """Set the environment of the tracers, before GStreamer is initialised: `GST_TRACERS`, and
    no colours in the log. Not `GST_DEBUG`, which would have GStreamer's default logger write
    the tracers' lines to stderr while it initialises, before the collector can take over:
    `Collector.get` turns the category on. `GST_DEBUG_FILE` is the user's to set."""
    for asked, kept in add_tracers([*CORE, *RUST]).items():
        logger.warning("GST_TRACERS already has %r: keeping it rather than %r", kept, asked)
    os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")


class Subscription:
    """What one consumer of the `Collector` sees: everything measured since its own last `take`,
    whatever the other consumers took in between."""

    def __init__(self, collector: "Collector") -> None:
        self._collector = collector
        self._samples: list[Sample] = []
        self._counts: dict[str, list[int]] = {}
        self._lost = collector.tail.lost

    def take(self) -> tuple[list[Sample], dict[str, tuple[int, int]]]:
        """The samples since the last call (or `start`), with every record written before it,
        and the buffers and bytes per `element:pad` (see `TracerLog.take`)."""
        return self._collector._take(self)

    def stop(self) -> None:
        """Stop taking, forgetting what was not taken. Once the last one has stopped, the
        collector keeps nothing."""
        self._collector._stop(self)

    @property
    def lost(self) -> int:
        """Times a ring of the debug log dropped lines before they were read, since `start`: the
        samples of those lines are missing"""
        return self._collector.tail.lost - self._lost


class Collector:
    """What the tracers measured, for any number of consumers: each `start`s a `Subscription`
    and `take`s what was measured since its own last take, as when a player measures while a
    test suite does. Get the one of the process with `Collector.get`: it raises `FeatureError`
    when the tracers are not running or their records are not in the log."""

    _instance: "Collector | None" = None
    _failure: FeatureError | None = None
    """What kept the first one from starting, once its log was set up: that cannot be undone, so
    a second try raises the same rather than set it up again and echo each line twice"""

    @classmethod
    def get(cls) -> "Collector":
        if cls._failure is not None:
            raise cls._failure
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        require("perf", map(_name, CORE), "perf")
        rust = installed(RUST)
        if missing := [name for name in RUST if name not in rust]:
            logger.warning(
                "GStreamer has no tracer %s: perf goes on without what it measures; the "
                "rstracers plugin of gst-plugins-rs has them",
                ", ".join(map(repr, missing)),
            )
        require("perf", rust, "perf")
        active = active_tracers()
        self._rust: dict[str, Any] = {name: active[name] for name in rust}
        self._folder = Path(tempfile.mkdtemp(prefix="mixtape-perf-"))  # the `write-log` files
        self._lock = threading.Lock()  # the subscriptions, and reading for them
        self._subscriptions: list[Subscription] = []
        # a `GST_DEBUG_FILE` is the user's: GStreamer writes it, the tail only reads
        self.tail = RingTail(None if os.environ.get("GST_DEBUG_FILE") else sys.stderr)
        entries = os.environ.get("GST_DEBUG", "").split(",")
        ours = [entry for entry in entries if entry.partition(":")[0] == "GST_TRACER"]
        if not ours:  # a `GST_DEBUG` that names it wins
            Gst.debug_set_threshold_for_name("GST_TRACER", Gst.DebugLevel.TRACE)
        elif ours != [LOG]:
            logger.warning("GST_DEBUG already has %r: keeping it", ",".join(ours))
        self.tracer_entries = [
            entry
            for entry in _entries(os.environ.get("GST_TRACERS", ""))
            if _name(entry) in self.tracers
        ]
        """The `GST_TRACERS` entries of the tracers it reads, as GStreamer got them"""
        atexit.register(self._close)
        # `time.monotonic_ns()` as GStreamer initialised, the start of its tracers' clock (that
        # clock is `gst_util_get_timestamp`, the monotonic clock Python reads too)
        try:
            self._init_ns = self._calibrate()
        except FeatureError as exc:
            type(self)._failure = exc  # the tail goes on echoing, as the default logger is gone
            raise

    def _calibrate(self) -> int:
        """`time.monotonic_ns()` as GStreamer initialised, which it does not tell: from the ts
        of the `new-element` record of an element made for it."""
        name = f"mixtape-perf-clock-{os.getpid()}"
        before = time.monotonic_ns()
        Gst.ElementFactory.make("identity", name)
        after = time.monotonic_ns()
        self.tail.catch_up()  # GStreamer wrote the record as it made the element
        if (ts := self.tail.parser.created.get(name)) is None:
            raise FeatureError(
                "The feature 'perf' finds no tracer records in the debug log: GST_DEBUG has "
                "to log GST_TRACER at level 7"
            )
        return (before + after) // 2 - ts

    def unknown(self, names: Iterable[str]) -> list[str]:
        """Those of the element `names` that it saw no creation of (`new-element`): made before
        it started, so what the tracers measure of them has no name."""
        self.tail.catch_up()
        created = self.tail.parser.created  # for good: `reset` keeps the names
        return sorted({name for name in names if name not in created})

    def _close(self) -> None:
        with contextlib.suppress(Exception):
            self.tail.close()  # echoes the last lines
        shutil.rmtree(self._folder, ignore_errors=True)

    def now(self) -> int:
        """Nanoseconds since GStreamer initialised."""
        return time.monotonic_ns() - self._init_ns

    def start(self) -> Subscription:
        """A consumer of what is measured from now on, not of what was before."""
        with self._lock:
            if self._subscriptions:
                self._pull()  # what the others have to get, not the newcomer
            else:
                self._flush_rust()
                self.tail.collect(True)
            subscription = Subscription(self)
            self._subscriptions.append(subscription)
            return subscription

    def _stop(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription not in self._subscriptions:
                return
            self._subscriptions.remove(subscription)
            if not self._subscriptions:
                self.tail.collect(False)
                self._flush_rust()

    def _take(self, subscription: Subscription) -> tuple[list[Sample], dict[str, tuple[int, int]]]:
        with self._lock:
            if subscription in self._subscriptions:
                self._pull()
            samples, subscription._samples = subscription._samples, []
            counts = {pad: (c[0], c[1]) for pad, c in subscription._counts.items()}
            for c in subscription._counts.values():
                c[0] = c[1] = 0
            return samples, counts

    def _pull(self) -> None:
        """Read what was measured since the last read, once, and hand it to every consumer."""
        samples = self._flush_rust()
        logged, counts = self.tail.take()
        samples += logged
        for subscription in self._subscriptions:
            subscription._samples += samples
            for pad, (buffers, size) in counts.items():
                total = subscription._counts.setdefault(pad, [0, 0])
                total[0] += buffers
                total[1] += size

    def _flush_rust(self) -> list[Sample]:
        """What the gst-plugins-rs tracers measured since the last call: `write-log` writes
        it to a file, and forgets it."""
        samples: list[Sample] = []
        for name, tracer in self._rust.items():
            path = self._folder / f"{name}.csv"
            tracer.emit("write-log", str(path))  # appends, after the first time
            with open(path, encoding="utf-8", errors="replace") as file:
                lines = file.readlines()
            os.truncate(path, 0)
            samples += RUST[name](lines)
        return samples

    @property
    def tracers(self) -> list[str]:
        """The names of the tracers whose measures it reads, core and gst-plugins-rs."""
        return [*map(_name, CORE), *self._rust]
