"""One subclassable asyncio player for a GStreamer pipeline."""

import asyncio
import concurrent.futures
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Self

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from . import events
from .exceptions import (
    MixTapeError,
    PlayerDescriptionError,
    PlayerNotConfigured,
    PlayerPipelineError,
    PlayerSetStateError,
)

logger = logging.getLogger(__name__)

MessageCallback = Callable[[Gst.Message], None]


def ensure_init() -> None:
    """Initialise GStreamer, once."""
    if not Gst.is_initialized():
        Gst.init(None)


class Player:
    """Drive a `Gst.Pipeline` from an asyncio loop.

    The player reads the pipeline's bus on the loop it is attached to,
    which happens lazily on the first `await` of any of its methods.
    Subclass it and override the state methods, or react to bus messages
    with `add_handler` and `events()`.
    """

    def __init__(self, pipeline: Gst.Pipeline) -> None:
        ensure_init()
        if not isinstance(pipeline, Gst.Pipeline):
            raise TypeError(f"pipeline must be a Gst.Pipeline, not {type(pipeline).__name__}")
        self.pipeline = pipeline
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fd: int | None = None
        self._error: PlayerPipelineError | None = None
        self._target = Gst.State.NULL  # what the caller asked for, to decide bus policy
        self._live = False
        self._buffering = False
        self._state_waiters: list[tuple[Gst.State, asyncio.Future[None]]] = []
        self._eos_waiters: list[asyncio.Future[None]] = []
        self._handlers: dict[object, tuple[Gst.MessageType, MessageCallback]] = {}
        self._events = events.Broadcast()

    @classmethod
    def from_description(cls, description: str) -> Self:
        """Build a player from a `gst-launch` style pipeline description."""
        ensure_init()
        try:
            element = Gst.parse_launch(description)
        except GLib.Error as exc:
            raise PlayerDescriptionError(
                f"Invalid pipeline {description!r}: {exc.message}"
            ) from exc
        if not isinstance(element, Gst.Pipeline):
            pipeline = Gst.Pipeline.new(None)
            pipeline.add(element)
            element = pipeline
        return cls(element)

    @classmethod
    def run(cls, description_or_pipeline: str | Gst.Pipeline, timeout: float | None = None) -> None:
        """Play a pipeline until EOS and tear it down, for scripts.

        Raises `PlayerPipelineError` on a pipeline error and `TimeoutError`
        when `timeout` seconds pass first.
        """
        if isinstance(description_or_pipeline, str):
            player = cls.from_description(description_or_pipeline)
        else:
            player = cls(description_or_pipeline)

        async def main() -> None:
            async with player:
                await asyncio.wait_for(player.play_until_eos(), timeout)

        asyncio.run(main())

    # -- inspection -- #

    @property
    def bus(self) -> Gst.Bus:
        """The pipeline's bus"""
        return self.pipeline.get_bus()

    @property
    def state(self) -> Gst.State:
        """The current pipeline state, asked from the pipeline"""
        return self.pipeline.get_state(0)[1]

    @property
    def pending(self) -> Gst.State:
        """The pending pipeline state, `VOID_PENDING` when not changing"""
        return self.pipeline.get_state(0)[2]

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The asyncio loop the player is attached to"""
        return self._loop

    @property
    def error(self) -> PlayerPipelineError | None:
        """The last pipeline error"""
        return self._error

    @property
    def sinks(self) -> list[Gst.Element]:
        """Returns all sink elements"""
        return list(self.pipeline.iterate_sinks())

    @property
    def sources(self) -> list[Gst.Element]:
        """Return all source elements"""
        return list(self.pipeline.iterate_sources())

    @property
    def elements(self) -> list[Gst.Element]:
        """Return all pipeline elements"""
        return list(self.pipeline.iterate_elements())

    def get_elements_by_gtype(self, gtype: Any) -> list[Gst.Element]:
        """Return all elements in pipeline that match gtype"""
        return [e for e in self.elements if e.get_factory().get_element_type() == gtype]

    # -- attaching to a loop -- #

    def setup(self) -> None:
        """Attach to the running loop. Idempotent; every async method calls it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise PlayerNotConfigured("setup needs a running asyncio loop") from exc
        if self._loop is not None and self._loop.is_closed():
            self._loop = self._fd = None  # the loop died without a teardown
        if self._loop is loop:
            return
        if self._loop is not None:
            raise PlayerNotConfigured("The player is attached to a different loop")
        fd = self.bus.get_pollfd().fd
        loop.add_reader(fd, self._drain)
        self._loop, self._fd = loop, fd

    def teardown(self) -> None:
        """Set the pipeline to NULL and detach from the loop. Idempotent, re-usable after."""
        self.pipeline.set_state(Gst.State.NULL)
        self._target = Gst.State.NULL
        self._live = self._buffering = False
        loop, fd = self._loop, self._fd
        self._loop = self._fd = None
        waiters = [future for _, future in self._state_waiters] + self._eos_waiters
        self._state_waiters.clear()
        self._eos_waiters.clear()
        if loop is not None and not loop.is_closed():
            if fd is not None:
                loop.remove_reader(fd)
            for future in waiters:
                future.cancel()
            self._events.close()
        else:
            self._events = events.Broadcast()  # its subscribers died with the loop

    async def __aenter__(self) -> Self:
        self.setup()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.teardown()

    def call_soon_threadsafe(self, callback: Callable[..., object], *args: object) -> None:
        """Run `callback(*args)` on the player's loop, from any thread."""
        self._attached_loop().call_soon_threadsafe(callback, *args)

    def run_coroutine_threadsafe[T](
        self, coro: Coroutine[Any, Any, T]
    ) -> concurrent.futures.Future[T]:
        """Run `coro` on the player's loop, from any thread."""
        return asyncio.run_coroutine_threadsafe(coro, self._attached_loop())

    def _attached_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            raise PlayerNotConfigured("The player is not attached to a running loop")
        return self._loop

    # -- state -- #

    async def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        """Set the pipeline state, returning once the change is complete."""
        self.setup()
        if self.state == state and self.pending == Gst.State.VOID_PENDING:
            return Gst.StateChangeReturn.SUCCESS
        error = self._error
        future = asyncio.get_running_loop().create_future()
        waiter = (state, future)
        self._state_waiters.append(waiter)
        self._target = state
        self._buffering = False
        try:
            ret = self.pipeline.set_state(state)
            if state == Gst.State.NULL:
                self._live = False
            elif ret == Gst.StateChangeReturn.NO_PREROLL:
                self._live = True
            if ret == Gst.StateChangeReturn.ASYNC:
                await future
            elif ret == Gst.StateChangeReturn.FAILURE:
                self._target = self.state
                self._drain()  # the element's ERROR is more useful than a bare failure
                if self._error is not error and self._error is not None:
                    raise self._error
                raise PlayerSetStateError(f"Could not set the pipeline to {state.value_nick}")
            return ret
        finally:
            with contextlib.suppress(ValueError):  # teardown may have cleared it
                self._state_waiters.remove(waiter)

    async def ready(self) -> Gst.StateChangeReturn:
        return await self.set_state(Gst.State.READY)

    async def pause(self) -> Gst.StateChangeReturn:
        return await self.set_state(Gst.State.PAUSED)

    async def play(self) -> Gst.StateChangeReturn:
        return await self.set_state(Gst.State.PLAYING)

    async def stop(self, eos: bool = False, timeout: float | None = 5.0) -> None:
        """Set the pipeline to NULL, after an EOS when `eos` and it is running."""
        self.setup()
        if eos and self.state in (Gst.State.PAUSED, Gst.State.PLAYING):
            try:
                await self.send_eos(timeout)
            except TimeoutError:
                logger.warning("No EOS after %s s, stopping anyway", timeout)
            except PlayerPipelineError:
                pass  # already logged and the pipeline is already NULL
        await self.set_state(Gst.State.NULL)

    async def send_eos(self, timeout: float | None = None) -> None:
        """Send an EOS event and wait for the EOS message it causes."""
        self.setup()
        future = asyncio.get_running_loop().create_future()
        self._eos_waiters.append(future)
        try:
            if not self.pipeline.send_event(Gst.Event.new_eos()):
                raise MixTapeError("The pipeline did not handle the EOS event")
            await asyncio.wait_for(future, timeout)
        finally:
            self._discard_eos_waiter(future)

    async def play_until_eos(self) -> None:
        """Play and return on the next EOS; raise `PlayerPipelineError` on an error."""
        self.setup()
        future = asyncio.get_running_loop().create_future()
        self._eos_waiters.append(future)  # before playing, a short stream may end meanwhile
        try:
            await self.play()
            await future
        finally:
            self._discard_eos_waiter(future)

    def _discard_eos_waiter(self, future: asyncio.Future[None]) -> None:
        future.cancel()
        with contextlib.suppress(ValueError):  # teardown may have cleared it
            self._eos_waiters.remove(future)

    # -- events and message handlers -- #

    def events(self) -> events.EventStream:
        """Subscribe to the events published from now on."""
        self.setup()
        return self._events.subscribe()

    def add_handler(
        self, message_type: Gst.MessageType, callback: MessageCallback
    ) -> Callable[[], None]:
        """Call `callback(message)` on the loop for bus messages of `message_type`,
        after the built-in handling. Returns a function that removes it again."""
        token = object()
        self._handlers[token] = (message_type, callback)

        def remove() -> None:
            self._handlers.pop(token, None)

        return remove

    # -- bus message handling -- #

    def _drain(self) -> None:
        """The loop's reader callback: handle every message on the bus."""
        while (message := self.bus.pop()) is not None:
            self._handle(message)

    def _handle(self, message: Gst.Message) -> None:
        kind = message.type
        if kind == Gst.MessageType.ERROR:
            self._on_error(message)
        elif kind == Gst.MessageType.EOS:
            self._on_eos()
        elif kind == Gst.MessageType.STATE_CHANGED:
            self._on_state_changed(message)
        elif kind == Gst.MessageType.WARNING:
            self._on_warning(message)
        elif kind == Gst.MessageType.CLOCK_LOST:
            self._on_clock_lost()
        elif kind == Gst.MessageType.LATENCY:
            self.pipeline.recalculate_latency()
        elif kind == Gst.MessageType.BUFFERING:
            self._on_buffering(message)
        elif kind == Gst.MessageType.QOS:
            logger.debug("QoS from %s", message.src.get_name())
        self._events.publish(events.Message(message))
        for message_type, callback in tuple(self._handlers.values()):
            if kind & message_type:
                try:
                    callback(message)
                except Exception:
                    logger.exception("Message handler %r failed", callback)

    def _on_error(self, message: Gst.Message) -> None:
        gerror, debug = message.parse_error()
        source = message.src.get_name() if message.src else "unknown"
        error = PlayerPipelineError(source, gerror.message, debug, gerror)
        self._error = error
        logger.error("Error from element %s: %s (%s)", source, gerror.message, debug)
        self._target = Gst.State.NULL
        self._live = self._buffering = False
        self.pipeline.set_state(Gst.State.NULL)
        for future in [future for _, future in self._state_waiters] + self._eos_waiters:
            if not future.done():
                future.set_exception(error)
                future.exception()  # mark it retrieved: nobody may be awaiting it
        self._events.publish(events.Error(error))

    def _on_eos(self) -> None:
        logger.info("EOS")
        for future in self._eos_waiters:
            if not future.done():
                future.set_result(None)
        self._events.publish(events.Eos())

    def _on_state_changed(self, message: Gst.Message) -> None:
        if message.src != self.pipeline:
            return
        old, new, pending = message.parse_state_changed()
        logger.debug("State changed from %s to %s", old.value_nick, new.value_nick)
        if pending == Gst.State.VOID_PENDING:
            _, state, still_pending = self.pipeline.get_state(0)  # the message may be stale
            for target, future in self._state_waiters:
                if target == new == state and still_pending == pending and not future.done():
                    future.set_result(None)
        self._events.publish(events.StateChanged(old, new, pending))

    def _on_warning(self, message: Gst.Message) -> None:
        gerror, debug = message.parse_warning()
        source = message.src.get_name() if message.src else "unknown"
        logger.warning("Warning from element %s: %s (%s)", source, gerror.message, debug)
        self._events.publish(events.Warning(source, gerror.message, debug))

    def _on_clock_lost(self) -> None:
        if self._target == Gst.State.PLAYING:
            logger.info("Clock lost, restarting the clock selection")
            self.pipeline.set_state(Gst.State.PAUSED)
            self.pipeline.set_state(Gst.State.PLAYING)

    def _on_buffering(self, message: Gst.Message) -> None:
        if self._live:
            return
        percent = message.parse_buffering()
        if percent < 100 and not self._buffering and self._target == Gst.State.PLAYING:
            self._buffering = True
            self.pipeline.set_state(Gst.State.PAUSED)
        elif percent == 100 and self._buffering:
            self._buffering = False
            if self._target == Gst.State.PLAYING:
                self.pipeline.set_state(Gst.State.PLAYING)
