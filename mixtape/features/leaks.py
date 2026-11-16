"""The `leaks` feature: which GStreamer objects are alive, by type, from GStreamer's own tracer."""

import asyncio
import logging
import os
from collections import Counter
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst
from pydantic import PositiveFloat

from ..exceptions import FeatureError
from ..model import command, prop
from . import Feature

if TYPE_CHECKING:
    from ..player import Player

logger = logging.getLogger(__name__)

TRACER = "leaks"
SINGLETONS = frozenset({"GstSystemClock", "GstTaskPool"})  # live for the whole process


def _tracers(value: str) -> list[str]:
    """The names in a `GST_TRACERS` value (`name(params);name`)."""
    return [item.partition("(")[0] for item in value.split(";") if item]


def _active_tracer() -> Any:
    """The leaks tracer GStreamer instantiated, or `FeatureError`: tracers are created by
    `Gst.init` from `GST_TRACERS` and never later."""
    for tracer in Gst.tracing_get_active_tracers():
        if tracer.__gtype__.name == "GstLeaksTracer":
            return tracer
    raise FeatureError(
        "The feature 'leaks' needs the leaks tracer, but GStreamer was initialised before it "
        "could set GST_TRACERS: build the player with Player.from_description(..., "
        "features=['leaks']) or run `mixtape run --feature leaks` before anything else "
        "initialises GStreamer, or set GST_TRACERS=leaks yourself"
    )


class Leaks(Feature):
    """Track the objects GStreamer creates and frees, to see what a long run keeps.

    It reads checkpoints of GStreamer's `leaks` tracer every `interval` seconds and keeps the
    objects created since it attached and not yet freed, by type. A count that only grows while
    the pipeline runs in a steady state is a leak.

    When the player is torn down, a warning names what is still alive once the pipeline is in
    NULL, apart from the process singletons (`GstSystemClock`, `GstTaskPool`): a leak shows at
    the end of a run, such as `mixtape run`, without reading `alive`.

    The tracer is one per process and exists only when `GST_TRACERS` names it before
    `Gst.init`: this feature sets that in `prepare`, so it works for a player built by
    `Player.from_description` (and `mixtape run`) first in the process. One player at a time
    can track, as they share the tracer's checkpoints.

    The tracer's `get-live-objects` signal is never used: PyGObject frees the objects it
    returns, which are still in the pipeline.
    """

    name = "leaks"
    requires = (TRACER,)

    def __init__(self, interval: PositiveFloat = 1.0) -> None:
        self.interval = interval
        """Seconds between the checkpoints taken while attached"""
        self._alive: Counter[str] = Counter()
        self._mark: Counter[str] = Counter()  # `alive` at the last `checkpoint` command
        self._tracer: Any = None
        self._task: asyncio.Task[None] | None = None

    def prepare(self) -> None:
        tracers = os.environ.get("GST_TRACERS", "")
        if TRACER not in _tracers(tracers):
            os.environ["GST_TRACERS"] = f"{tracers};{TRACER}" if tracers else TRACER

    def check(self, player: "Player") -> None:
        _active_tracer()

    def attach(self, player: "Player") -> None:
        self._tracer = _active_tracer()
        self._alive.clear()
        self._mark.clear()
        self._tracer.emit("activity-start-tracking")
        self._task = asyncio.get_running_loop().create_task(self._poll())

    def detach(self, player: "Player") -> None:
        if self._task is not None and not self._task.get_loop().is_closed():
            self._task.cancel()  # a closed loop cannot cancel: the task died with it
        self._task = None
        if self._tracer is not None:
            self._fold()  # a final checkpoint, for what the teardown freed and what it kept
            leaked = {k: n for k, n in self._alive.most_common() if k not in SINGLETONS}
            if leaked and player.state == Gst.State.NULL:  # else it still holds its buffers
                logger.warning(
                    "Still alive after the run: %s",
                    ", ".join(f"{count} {name}" for name, count in leaked.items()),
                )
            self._tracer.emit("activity-stop-tracking")
            self._tracer = None
        self._alive.clear()
        self._mark.clear()

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if self._fold():
                self.notify("alive")

    def _fold(self) -> bool:
        """Take a checkpoint of the tracer and add what it saw to `alive`. Whether it changed.

        A checkpoint covers what happened since the previous one. Objects freed that were
        created before tracking started would make a count negative: those are dropped."""
        if self._tracer is None:
            return False
        checkpoint = self._tracer.emit("activity-get-checkpoint")
        before = Counter(self._alive)
        for created in checkpoint.get_value("objects-created-list") or ():
            self._alive[created.get_string("type-name")] += 1
        for removed in checkpoint.get_value("objects-removed-list") or ():
            self._alive[removed.get_string("type-name")] -= 1
        self._alive = +self._alive  # drops the zero and negative counts
        return self._alive != before

    @prop
    def alive(self) -> dict[str, int]:
        """Objects created since tracking started and not yet freed, by type"""
        return dict(self._alive.most_common())

    @command
    def checkpoint(self) -> dict[str, int]:
        """The change in live objects by type since the last checkpoint"""
        if self._tracer is None:
            return {}  # not tracking: the player is not set up
        if self._fold():
            self.notify("alive")
        change = {
            type_name: delta
            for type_name in self._alive.keys() | self._mark.keys()
            if (delta := self._alive[type_name] - self._mark[type_name])
        }
        self._mark = Counter(self._alive)
        return dict(sorted(change.items(), key=lambda item: -abs(item[1])))

    @command
    def reset(self) -> None:
        """Forget the live objects counted so far, and go on tracking"""
        self._fold()  # what happened until now is forgotten too
        self._alive.clear()
        self._mark.clear()
        self.notify("alive")
