"""The `stats` feature: buffers and bits per second leaving each exposed element."""

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst
from pydantic import BaseModel, PositiveFloat

from ..model import prop
from ..player import _retrying
from . import Feature

if TYPE_CHECKING:
    from ..player import Player


class Rate(BaseModel):
    """What leaves an element, per second"""

    buffers_per_s: float
    kbit_per_s: float


class Stats(Feature):
    """Measure the data flow of the pipeline: buffers and kilobits per second out of each
    exposed element, from a buffer probe on its source pads.

    Only the pads an element has when the player is set up are probed: the ones a demuxer or
    decodebin adds later are not, and neither are bins. An element with no source pad (a sink)
    has no rate.
    """

    name = "stats"

    def __init__(self, interval: PositiveFloat = 1.0) -> None:
        self.interval = interval
        """Seconds between the rates published while attached"""
        self._rates: dict[str, Rate] = {}
        self._probes: list[tuple[Any, int]] = []  # (pad, probe id)
        self._lock = threading.Lock()  # the probes run on streaming threads
        self._counts: dict[str, list[int]] = {}  # per element: [buffers, bytes], under the lock
        self._task: asyncio.Task[None] | None = None

    def attach(self, player: "Player") -> None:
        for key, element in player.exposed_elements().items():
            # an iterator, not `srcpads`: a streaming thread may add a pad meanwhile
            pads = [] if isinstance(element, Gst.Bin) else _retrying(element.iterate_src_pads)
            if not pads:
                continue
            counts = self._counts[key] = [0, 0]
            for pad in pads:
                probe = Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST
                self._probes.append((pad, pad.add_probe(probe, self._count, counts)))
        self._task = asyncio.get_running_loop().create_task(self._poll())

    def detach(self, player: "Player") -> None:
        if self._task is not None and not self._task.get_loop().is_closed():
            self._task.cancel()  # a closed loop cannot cancel: the task died with it
        self._task = None
        for pad, probe in self._probes:
            pad.remove_probe(probe)
        self._probes.clear()
        self._counts.clear()
        self._rates = {}

    def _count(self, pad: Any, info: Any, counts: list[int]) -> Any:
        """On a streaming thread: only count, the loop does the rest."""
        if info.type & Gst.PadProbeType.BUFFER_LIST:
            buffers = info.get_buffer_list()
            number = buffers.length()
            size = sum(buffers.get(i).get_size() for i in range(number))
        else:
            number, size = 1, info.get_buffer().get_size()
        with self._lock:
            counts[0] += number
            counts[1] += size
        return Gst.PadProbeReturn.OK

    async def _poll(self) -> None:
        last = self._read()
        then = time.monotonic()
        while True:
            await asyncio.sleep(self.interval)
            current, now = self._read(), time.monotonic()
            elapsed = now - then
            self._rates = {
                key: Rate(
                    buffers_per_s=round((buffers - last[key][0]) / elapsed, 1),
                    kbit_per_s=round((size - last[key][1]) * 8 / 1000 / elapsed, 1),
                )
                for key, (buffers, size) in current.items()
            }
            last, then = current, now
            self.notify("rates")

    def _read(self) -> dict[str, tuple[int, int]]:
        with self._lock:
            return {key: (counts[0], counts[1]) for key, counts in self._counts.items()}

    @prop
    def rates(self) -> dict[str, Rate]:
        """Buffers and kilobits per second leaving each exposed element, over the last interval"""
        return self._rates
