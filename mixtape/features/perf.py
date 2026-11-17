"""The `perf` feature: measure a running pipeline with GStreamer's own tracers, for the live
page (`perf.summary`) and for CI (the artifacts of `out`).

Python never runs on a streaming thread for it: the `Collector` of `mixtape.perf.collect` reads
the records of the core `latency` and `stats` tracers from GStreamer's ring buffer logger, and
asks the gst-plugins-rs tracers to write theirs to a file (`write-log`)."""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import PositiveFloat

from ..exceptions import FeatureError
from ..model import prop
from ..perf import Window
from ..perf.collect import CORE, Collector, prepare
from ..perf.parse import RUST
from ..perf.record import Recorder
from . import Feature
from .tracers import _name


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
    own_tracers = (*(_name(entry) for entry in CORE), *RUST)

    def __init__(self, out: Path | None = None, interval: PositiveFloat = 1.0) -> None:
        self.out = out
        """The folder of the artifacts, or None for none"""
        self.interval = interval
        """Seconds per window"""
        self._recorder: Recorder | None = None
        self._summary: Window | None = None
        self._graph: dict[str, Any] | None = None  # for the report: the latest snapshot
        self._snapshot_failed = False
        self._task: asyncio.Task[None] | None = None

    def prepare(self) -> None:
        prepare()

    def initialised(self) -> None:
        Collector.get()  # before the pipeline's elements: their names are in the log

    def check(self, player: "Player") -> None:
        unknown = Collector.get().unknown(e.get_name() for e in player._element_keys().values())
        if unknown:
            raise FeatureError(
                f"The feature 'perf' has no record of the creation of "
                f"{', '.join(map(repr, unknown))}: the pipeline was built before it set up the "
                "debug log, so its measures would have no names; build the player with "
                "Player.from_description or `mixtape run`"
            )

    def attach(self, player: "Player") -> None:
        self._recorder = Recorder(self.out)
        self._snapshot_failed = False
        self._snapshot(player)
        self._task = asyncio.get_running_loop().create_task(self._poll(player))

    def detach(self, player: "Player") -> None:
        if self._task is not None and not self._task.get_loop().is_closed():
            self._task.cancel()  # a closed loop cannot cancel: the task died with it
        self._task = None
        self._summary = None
        recorder, self._recorder = self._recorder, None
        assert recorder is not None  # `attach` ran
        try:  # what the run did since the last window, and its end
            recorder.window(_keys(player))
        finally:
            recorder.finish(
                {
                    "pipeline": player._description,
                    "interval": self.interval,
                    "graph": self._graph,
                }
            )
            self._graph = None

    async def _poll(self, player: "Player") -> None:
        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            due += self.interval
            await asyncio.sleep(max(due - loop.time(), 0))
            keys = _keys(player)
            self._snapshot(player)
            assert self._recorder is not None
            self._summary = await asyncio.to_thread(self._recorder.window, keys)
            self.notify("summary")

    def _snapshot(self, player: "Player") -> None:
        """Keep the pipeline's graph for the report of `out`, as of now: each window takes one,
        as elements come with the stream (a decodebin's) and caps with negotiation, and both
        are gone by `detach`, in NULL."""
        if self.out is None:
            return
        try:
            self._graph = player.graph().model_dump()
        except Exception:
            if not self._snapshot_failed:  # once a run, not every window
                logger.exception("perf could not snapshot the pipeline's graph for its report")
            self._snapshot_failed = True

    @prop
    def summary(self) -> Window | None:
        """The statistics of the last window, by metric and subject, while attached"""
        return self._summary
