import asyncio
import logging
from typing import Any, Optional, Type, TypeVar, Tuple, Callable, List

import attr
from pampy import match, ANY

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa

from .base import BasePlayer
from .exceptions import PlayerAlreadyConfigured, PlayerNotConfigured, PlayerPipelineError
from .events import PlayerEvents


logger = logging.getLogger(__name__)

AsyncPlayerType = TypeVar("AsyncPlayerType", bound="AsyncPlayer")


@attr.s
class AsyncPlayer(BasePlayer):

    futures: List[Any] = attr.ib(init=False, default=attr.Factory(list))
    events: PlayerEvents = attr.ib(init=False, default=attr.Factory(PlayerEvents))
    loop: Optional[asyncio.AbstractEventLoop] = attr.ib(init=False, default=None, repr=False)
    pollfd: Any = attr.ib(init=False, default=None, repr=False)

    # -- overriding base state shortcuts and pipeline methods with asyncio -- #

    async def ready(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        ret = super().ready()
        await self.events.state.wait_for(self.events.state(Gst.State.READY))
        return ret

    async def play(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        ret = super().play()
        await self.events.wait_for_state(Gst.State.PLAYING)
        return ret

    async def pause(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        ret = super().pause()
        await self.events.state.wait_for(self.events.state(Gst.State.PAUSED))
        return ret

    async def stop(
        self, send_eos: bool = True, teardown: bool = False
    ) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        if send_eos:
            await self.send_eos()
        ret = self.set_state(Gst.State.NULL)

        if teardown:
            self.teardown()

        return ret

    async def send_eos(self) -> bool:  # type: ignore
        ret = self.pipeline.send_event(Gst.Event.new_eos())
        await self.events.eos.wait()
        return ret

    # -- bus message handling -- #

    def handle(self) -> None:
        """
        Asyncio reader callback, called when a message is available on
        the bus.
        """
        msg = self.bus.pop()
        if msg:
            # fmt: off
            handler = match(msg.type,
                Gst.MessageType.ERROR, lambda x: self.on_error,  # noqa: E128
                Gst.MessageType.EOS, lambda x: self.on_eos,
                Gst.MessageType.STATE_CHANGED, lambda x: self.on_state_changed,
                Gst.MessageType.ASYNC_DONE, lambda x: self.on_async_done,
                ANY, lambda x: self.on_unhandled_msg)
            # fmt: on
            handler(self.bus, msg)

    def on_state_changed(self, bus: Gst.Bus, message: Gst.Message) -> None:  # pylint: disable=unused-argument
        """
        Handler for `state_changed` messages
        By default will only log to `debug`
        """
        old, new, _ = message.parse_state_changed()

        if message.src != self.pipeline:
            return
        logger.debug(
            "State changed from %s to %s",
            Gst.Element.state_get_name(old),
            Gst.Element.state_get_name(new),
        )

        self.events.pick_state(new)

    def on_error(self, bus: Gst.Bus, message: Gst.Message) -> None:  # pylint: disable=unused-argument
        """
        Handler for `error` messages
        By default it will parse the error message,
        log to `error` and append to `self.errors`
        """
        err, debug = message.parse_error()
        # self.errors.append((err, debug))
        self.events.error.set()
        logger.error("Error received from element %s:%s", message.src.get_name(), err.message)
        if debug is not None:
            logger.error("Debugging information: %s", debug)
        raise PlayerPipelineError(err)

    def on_eos(self, bus: Gst.Bus, message: Gst.Message) -> None:  # pylint: disable=unused-argument
        """
        Handler for eos messages
        By default it sets the eos event
        """
        self.events.eos.set()
        logger.info("End-Of-Stream reached")

    def on_async_done(self, bus: Gst.Bus, message: Gst.Message) -> None:  # pylint: disable=unused-argument
        """
        Handler for `async_done` messages
        By default, it will pop any futures available in `self.futures` 
        and call their result.
        """
        logger.debug("Unhandled ASYNC_DONE message: %s", message.parse_async_done())

    def on_unhandled_msg(self, bus: Gst.Bus, message: Gst.Message) -> None:  # pylint: disable=unused-argument
        """
        Handler for all other messages.
        By default will just log with `debug`
        """
        logger.debug("Unhandled msg: %s", message.type)

    # -- setup and teaddown -- #

    def setup(self) -> None:
        """Setup needs a running asyncio loop"""
        super().setup()
        self.loop = asyncio.get_running_loop()
        self.pollfd = self.bus.get_pollfd()
        self.loop.add_reader(self.pollfd.fd, self.handle)
        self.events.setup.set()
        logger.debug("Setup complete")

    def teardown(self) -> None:
        """Cleanup player references to loop and gst resources"""
        super().teardown()
        self.loop.remove_reader(self.pollfd.fd)
        self.pollfd = None
        self.loop = None
        self.events.teardown.set()
        logger.debug("Teardown complete")

    # -- sync helpers -- #

    def call_play(self) -> Any:
        return asyncio.run_coroutine_threadsafe(self.play(), loop=self.loop)

    def call_stop(self) -> Any:
        return asyncio.run_coroutine_threadsafe(self.stop(), loop=self.loop)

    def call_pause(self) -> Any:
        return asyncio.run_coroutine_threadsafe(self.pause(), loop=self.loop)

    # -- util -- #

    def create_async_future(self):  # type: ignore
        """
        Creates a future and appends it to `futures`.
        Will `result` with a async_done message.
        """
        ft = self.loop.create_future()
        self.futures.append(ft)
        return ft
