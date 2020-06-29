import asyncio
import logging
from typing import Any, Optional, TypeVar, Tuple, List, Callable, MutableMapping
import attr

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .base import BasePlayer
from .exceptions import PlayerPipelineError, PlayerNotConfigured
from .events import PlayerEvents

logger = logging.getLogger(__name__)

AsyncPlayerType = TypeVar("AsyncPlayerType", bound="AsyncPlayer")


@attr.s
class AsyncPlayer(BasePlayer):
    """
    A asyncio compatible player.
    Interfaces with the `Gst.Bus` with an asyncio file descriptor,
    which is used to set `asyncio.Event` when received for the bus,
    allowing for asyncio compatible methods.
    """

    futures: List[Any] = attr.ib(init=False, default=attr.Factory(list))
    events: PlayerEvents = attr.ib(init=False, default=attr.Factory(PlayerEvents))
    loop: Optional[asyncio.AbstractEventLoop] = attr.ib(init=False, default=None, repr=False)
    pollfd: Any = attr.ib(init=False, default=None, repr=False)
    handlers: MutableMapping[Gst.MessageType, Callable[[Gst.Bus, Gst.Message], None]] = attr.ib(
        init=False, repr=False
    )

    # -- defaults -- #

    @handlers.default
    def _handlers(self) -> MutableMapping[Gst.MessageType, Callable[[Gst.Bus, Gst.Message], None]]:
        return {
            Gst.MessageType.QOS: self.on_qos,
            Gst.MessageType.ERROR: self.on_error,
            Gst.MessageType.EOS: self.on_eos,
            Gst.MessageType.STATE_CHANGED: self.on_state_changed,
            Gst.MessageType.ASYNC_DONE: self.on_async_done,
        }

    # -- overriding base state shortcuts and pipeline methods with asyncio -- #

    async def ready(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        """Async override of base.ready"""
        ret = super().ready()
        await self.events.wait_for_state(Gst.State.READY)
        return ret

    async def play(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        """Async override of base.play"""
        ret = super().play()
        await self.events.wait_for_state(Gst.State.PLAYING)
        return ret

    async def pause(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        """Async override of base.pause"""
        ret = super().pause()
        await self.events.wait_for_state(Gst.State.PAUSED)
        return ret

    # fmt: off
    async def stop(self, send_eos: bool = True, teardown: bool = False) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:  # type: ignore
        """Async override of base.stop"""
    # fmt: on
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
            handler = self.handlers.get(msg.type, self.on_unhandled_msg)
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

    def on_error(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for `error` messages
        By default it will parse the error message,
        log to `error` and append to `self.errors`
        """
        err, debug = message.parse_error()
        logger.error("Error received from element %s:%s on %s", message.src.get_name(), err.message, bus)
        if debug is not None:
            logger.error("Debugging information: %s", debug)

        self.teardown()
        self.events.error.set()
        raise PlayerPipelineError(err)

    def on_eos(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for eos messages
        By default it sets the eos event
        """
        self.events.eos.set()
        logger.info("EOS message: %s received from pipeline on %s", message, bus)

    def on_async_done(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for `async_done` messages
        By default, it will pop any futures available in `self.futures`
        and call their result.
        """
        msg = message.parse_async_done()
        logger.debug("Unhandled ASYNC_DONE message: %s on %s", msg, bus)

    def on_unhandled_msg(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for all other messages.
        By default will just log with `debug`
        """
        logger.debug("Unhandled msg: %s on %s", message.type, bus)

    def on_qos(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for `qos` messages
        By default it will parse the error message,
        log to `error` and append to `self.errors`
        """
        live, running_time, stream_time, timestamp, duration = message.parse_qos()
        logger.warning(
            "Qos message: live:%s - running:%s - stream:%s - timestamp:%s - duration:%s received from %s on %s",
            live,
            running_time,
            stream_time,
            timestamp,
            duration,
            message.src.get_name(),
            bus)

    # -- setup and teaddown -- #

    def setup(self) -> None:
        """Setup needs a running asyncio loop"""
        self.loop = asyncio.get_running_loop()
        self.pollfd = self.bus.get_pollfd()
        self.loop.add_reader(self.pollfd.fd, self.handle)
        super().setup()
        self.events.setup.set()
        logger.debug("Setup complete")

    def teardown(self) -> None:
        """Cleanup player references to loop and gst resources"""
        super().teardown()
        if self.loop:
            self.loop.remove_reader(self.pollfd.fd)
        self.pollfd = None
        self.loop = None
        self.events.teardown.set()
        logger.debug("Teardown complete")

    # -- sync helpers -- #

    def call_play(self) -> Any:
        """Convencience method to call play from another thread"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.play(), loop=self.loop)
        else:
            raise PlayerNotConfigured("Player setup without running loop not allowed")

    def call_stop(self) -> Any:
        """Convencience method to call stop from another thread"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.stop(), loop=self.loop)
        else:
            raise PlayerNotConfigured("Player setup without running loop not allowed")

    def call_pause(self) -> Any:
        """Convencience method to call pause from another thread"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.pause(), loop=self.loop)
        else:
            raise PlayerNotConfigured("Player setup without running loop not allowed")

    # -- util -- #

    def create_async_future(self):  # type: ignore
        """
        Creates a future and appends it to `futures`.
        Will `result` with a async_done message.
        """
        if self.loop:
            ft = self.loop.create_future()
        else:
            raise PlayerNotConfigured("Player setup without running loop not allowed")
        self.futures.append(ft)
        return ft
