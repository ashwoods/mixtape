import asyncio
import itertools
import logging
from typing import Any, Callable, List, MutableMapping, Tuple, Type, TypeVar

import attr
import gi
import pluggy

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .events import PlayerEvents
from .exceptions import PlayerNotConfigured, PlayerPipelineError, PlayerSetStateError


logger = logging.getLogger(__name__)

PlayerType = TypeVar("PlayerType", bound="Player")


# from . import hookspecs


class Context:
    """Plugin shared state object."""

    def __init__(self) -> None:
        self.properties: MutableMapping[str, Any] = dict()
        self.commands: MutableMapping[str, Any] = dict()

    def register_property(self, name: str, value: Any) -> None:
        self.properties[name] = value

    def register_command(self, name: str, value: Any) -> None:
        self.commands[name] = value

    def clear_commands(self):
        self.commands = {}


@attr.s
class Command:
    name: str = attr.ib()
    method: Callable = attr.ib()
    availability_check: Callable = attr.ib(default=None)

    def register_command(self, ctx):
        if self.availability_check and self.availability_check():
            ctx.register_command(self.name, self.method)


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
        if self.state is not Gst.State.NULL:
            self.teardown()

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
    async def create(cls: Type[PlayerType], pipeline: Gst.Pipeline) -> PlayerType:
        """Player factory from a given pipeline that calls setup by default"""
        player = cls(pipeline)
        player.setup()
        return player

    @classmethod
    async def from_description(cls: Type[PlayerType], description: str) -> PlayerType:
        """Player factory from a pipeline description"""
        return await cls.create(pipeline=cls.parse_description(description))

    @staticmethod
    def parse_description(description: str) -> Gst.Pipeline:
        pipeline = Gst.parse_launch(description)
        if not isinstance(pipeline, Gst.Pipeline):
            raise ValueError("Invalid pipeline description")
        return pipeline


class BoomBox:
    """
    Facade object that orchestrates plugin callbacks
    and exposes plugin commands and properties.
    """

    DEFAULT_PLAYER_COMMANDS: List[str] = ["play", "pause", "stop", "ready"]
    DEFAULT_PLAYER_ATTRIBUTES: List[Any] = []

    def __init__(self, player: Player, pm: Type[pluggy.PluginManager]):
        self._player = player
        self._pm = pm
        self._context = Context()
        # init all the plugins
        self._hook.mixtape_plugin_init(player=self._player, ctx=self._context)

        # rename and monkeypatch default set state
        self._player._set_state = self._player.set_state
        self._player.set_state = self.set_state
        # register initial commands
        self._register_commands()

    def __getattr__(self, name: str) -> Any:
        """
        Expose methods and properties from plugins
        """
        try:
            return {**self._context.properties, **self._context.commands}[name]
        except KeyError:
            raise AttributeError

    @property
    def _hook(self) -> Any:
        """Convenience shortcut for pm hook"""
        return self._pm.hook

    def _register_commands(self) -> None:
        # register all the commands
        self._context.clear_commands()
        for cmd in self.DEFAULT_PLAYER_COMMANDS:
            self._context.register_command(cmd, getattr(self._player, cmd))
        results = self._hook.mixtape_register_commands(player=self._player, ctx=self._context)
        results = list(itertools.chain(*results))
        for command in results:
            command.register_command(self._context)

    def setup(self) -> None:
        self._player.setup()
        self._hook.mixtape_setup(player=self._player, ctx=self._context)

    def teardown(self) -> None:
        self._hook.mixtape_teardown(player=self._player, ctx=self._context)
        self._player.teardown()

    async def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        self._hook.mixtape_before_state_changed(player=self._player, ctx=self._context, state=state)
        ret = await self._player._set_state(state)
        self._hook.mixtape_on_state_changed(player=self._player, ctx=self._context, state=state)
        logger.info("Registering commands on state change")
        self._register_commands()
        return ret
