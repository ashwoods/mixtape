import asyncio
import logging
import warnings
from typing import Any, Callable, List, MutableMapping, Tuple, Type, TypeVar

import attr
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .events import PlayerEvents
from .exceptions import PlayerNotConfigured, PlayerPipelineError, PlayerSetStateError

logger = logging.getLogger(__name__)

P = TypeVar("P", bound="Player")


@attr.s
class Player:
    """Player base player"""

    pipeline: Gst.Pipeline = attr.ib(validator=attr.validators.instance_of(Gst.Pipeline))
    events: PlayerEvents = attr.ib(init=False, default=attr.Factory(PlayerEvents))
    handlers: MutableMapping[Gst.MessageType, Callable[[Gst.Bus, Gst.Message], None]] = attr.ib(
        init=False, repr=False
    )

    @handlers.default
    def _handlers(self) -> MutableMapping[Gst.MessageType, Callable[[Gst.Bus, Gst.Message], None]]:
        return {
            Gst.MessageType.QOS: self._on_qos,
            Gst.MessageType.ERROR: self._on_error,
            Gst.MessageType.EOS: self._on_eos,
            Gst.MessageType.STATE_CHANGED: self._on_state_changed,
            Gst.MessageType.ASYNC_DONE: self._on_async_done,
        }

    def __del__(self) -> None:
        """
        Make sure that the gstreamer pipeline is always cleaned up
        """

    @property
    def bus(self) -> Gst.Bus:
        """Convenience property for the pipeline Gst.Bus"""
        return self.pipeline.get_bus()

    @property
    def state(self) -> Gst.State:
        """Convenience property for the current pipeline Gst.State"""
        return self.pipeline.get_state(0)[1]

    @property
    def sinks(self) -> List[Any]:
        """Returns all sink elements"""
        return list(self.pipeline.iterate_sinks())

    @property
    def sources(self) -> List[Any]:
        """Return all source elements"""
        return list(self.pipeline.iterate_sources())

    @property
    def elements(self) -> List[Any]:
        """Return all pipeline elements"""
        return list(self.pipeline.iterate_elements())

    def get_elements_by_gtype(self, gtype: Any) -> List[Any]:
        """Return all elements in pipeline that match gtype"""
        return [e for e in self.elements if e.get_factory().get_element_type() == gtype]

    def setup(self) -> None:
        """Setup needs a running asyncio loop"""
        loop = asyncio.get_running_loop()
        pollfd = self.bus.get_pollfd()
        loop.add_reader(pollfd.fd, self._handle)
        self.events.setup.set()

    def teardown(self) -> None:
        """Cleanup player references to loop and gst resources"""
        if self.state is not Gst.State.NULL:
            self.pipeline.set_state(Gst.State.NULL)
            logger.debug("Teardown set state to null")
        logger.debug("Removing pollfd")
        loop = asyncio.get_running_loop()
        pollfd = self.bus.get_pollfd()
        loop.remove_reader(pollfd.fd)
        self.events.teardown.set()

    # controls

    async def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        """Async set state"""
        if self.state == state:
            raise ValueError("Pipeline state is already in state %s.", state)
        if not self.events.setup.is_set():
            raise PlayerNotConfigured("Setting state before setup is not allowed.")
        ret = self.pipeline.set_state(state)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise PlayerSetStateError
        if ret == Gst.StateChangeReturn.ASYNC:
            await self.events.wait_for_state(state)
        return ret

    async def ready(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Async override of base.ready"""
        return await self.set_state(Gst.State.READY)

    async def play(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Async override of base.play"""
        return await self.set_state(Gst.State.PLAYING)

    async def pause(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Async override of base.pause"""
        return await self.set_state(Gst.State.PAUSED)

    async def stop(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Async override of base.stop"""
        return await self.set_state(Gst.State.NULL)

    # -- utility methods -- #

    async def send_eos(self) -> bool:
        """Send eos to pipeline and await event"""
        ret = self.pipeline.send_event(Gst.Event.new_eos())
        await self.events.eos.wait()
        return ret

    async def play_until_eos(self) -> None:
        """Play until eos or an error"""
        await self.play()
        await asyncio.wait(
            {self.events.eos.wait(), self.events.error.wait()}, return_when=asyncio.FIRST_COMPLETED
        )

    # -- bus message handling -- #

    def _handle(self) -> None:
        """
        Asyncio reader callback, called when a message is available on
        the bus.
        """
        msg = self.bus.pop()
        if msg:
            handler = self.handlers.get(msg.type, self._on_unhandled_msg)
            handler(self.bus, msg)

    def _on_state_changed(
        self, bus: Gst.Bus, message: Gst.Message
    ) -> None:  # pylint: disable=unused-argument
        """
        Handler for `state_changed` messages
        By default will only log to `debug`
        """
        old, new, _ = message.parse_state_changed()

        if message.src != self.pipeline:
            return
        logger.info(
            "State changed from %s to %s",
            Gst.Element.state_get_name(old),
            Gst.Element.state_get_name(new),
        )

        self.events.pick_state(new)

    def _on_error(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for `error` messages
        By default it will parse the error message,
        log to `error` and append to `self.errors`
        """
        err, debug = message.parse_error()
        logger.error(
            "Error received from element %s:%s on %s", message.src.get_name(), err.message, bus
        )
        if debug is not None:
            logger.error("Debugging information: %s", debug)

        self.teardown()
        self.events.error.set()
        raise PlayerPipelineError(err)

    def _on_eos(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for eos messages
        By default it sets the eos event
        """
        logger.info("Received EOS message on bus")
        self.events.eos.set()

    def _on_async_done(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for `async_done` messages
        By default, it will pop any futures available in `self.futures`
        and call their result.
        """
        msg = message.parse_async_done()
        logger.debug("Unhandled ASYNC_DONE message: %s on %s", msg, bus)

    def _on_unhandled_msg(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """
        Handler for all other messages.
        By default will just log with `debug`
        """
        logger.debug("Unhandled msg: %s on %s", message.type, bus)

    def _on_qos(self, bus: Gst.Bus, message: Gst.Message) -> None:
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
            bus,
        )

    @classmethod
    async def create(cls: Type[P], pipeline: Gst.Pipeline) -> P:
        """Player factory from a given pipeline that calls setup by default"""
        player = cls(pipeline)
        player.setup()
        return player

    @classmethod
    async def from_description(cls: Type[P], description: str) -> P:
        """Player factory from a pipeline description"""
        pipeline = Gst.parse_launch(description)
        if not isinstance(pipeline, Gst.Pipeline):
            raise ValueError("Invalid pipeline description")
        return await cls.create(pipeline)


class AsyncPlayer(Player):
    def __init_subclass__(cls) -> None:
        warnings.warn("Class has been renamed Player", DeprecationWarning, 2)
