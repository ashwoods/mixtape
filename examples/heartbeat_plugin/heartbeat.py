"""A feature that counts beats: `heartbeat.beats` and `heartbeat.reset`.

It is the module of the package in this folder (see pyproject.toml).
"""

# --8<-- [start:feature]
import asyncio
import os
from typing import Annotated

from pydantic import Field

from mixtape import Feature, Player, command, prop
from mixtape.exceptions import FeatureError


class Heartbeat(Feature):
    name = "heartbeat"
    requires = ("identity",)  # GStreamer elements or tracers it needs

    def __init__(self, interval: Annotated[float, Field(gt=0)] = 1.0) -> None:
        self.interval = interval
        self._beats = 0
        self._task: asyncio.Task[None] | None = None

    def prepare(self) -> None:
        # before GStreamer is initialised: the place for environment variables
        os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")

    def check(self, player: Player) -> None:
        # when the player is built: refuse a pipeline the feature cannot work with
        if not player.sinks:
            raise FeatureError("heartbeat needs a pipeline with a sink")

    def attach(self, player: Player) -> None:
        # when the player is set up, on its loop
        self._beats = 0
        self._task = asyncio.get_running_loop().create_task(self._beat())

    def detach(self, player: Player) -> None:
        # when it is torn down
        if self._task:
            self._task.cancel()

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._beats += 1
            self.notify("beats")

    @prop
    def beats(self) -> int:
        """Beats since the player was set up"""
        return self._beats

    @command
    def reset(self) -> None:
        """Start counting again"""
        self._beats = 0
        self.notify("beats")


# --8<-- [end:feature]
