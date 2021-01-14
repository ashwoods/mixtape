import asyncio
import enum

import attr
import beppu
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


class States(enum.Enum):
    """Python enum representing the Gst.Pipeline states"""

    VOID_PENDING = 0
    NULL = 1
    READY = 2
    PAUSED = 3
    PLAYING = 4


class SetupEvent(asyncio.Event):
    pass


class EosEvent(asyncio.Event):
    pass


class ErrorEvent(asyncio.Event):
    pass


class TearDownEvent(asyncio.Event):
    pass


@attr.s(auto_attribs=True, slots=True, frozen=True)
class PlayerEvents:
    """
    Player async event synchronization flags
    """

    # core events
    setup: asyncio.Event = attr.Factory(SetupEvent)
    eos: asyncio.Event = attr.Factory(EosEvent)
    error: asyncio.Event = attr.Factory(ErrorEvent)
    teardown: asyncio.Event = attr.Factory(TearDownEvent)

    # basket for state events
    state: beppu.Basket = attr.ib(init=False)

    @state.default
    def _get_state(self) -> beppu.Basket:
        return beppu.Basket(enum=States)

    def pick_state(self, state: Gst.State) -> None:
        self.state.pick(States(int(state)))

    async def wait_for_state(self, state: Gst.State) -> None:
        await self.state.wait_for(States(int(state)))
