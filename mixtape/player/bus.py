"""The player's bus: its reader on the loop, the dispatch of every message to the state machine
and the handlers, and the registry of those handlers."""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .. import events
from .state import StateMachine

logger = logging.getLogger("mixtape.player")  # the player's: the CLI filters on its name

MessageCallback = Callable[[Gst.Message], None]


class Host(Protocol):
    """What the bus needs of its player, read at each use."""

    @property
    def pipeline(self) -> Gst.Pipeline: ...

    @property
    def bus(self) -> Gst.Bus: ...

    def _publish(self, event: events.Event) -> None: ...


class Bus:
    """The bus of one player's pipeline, read on the loop the player is attached to."""

    def __init__(self, host: Host, machine: StateMachine) -> None:
        self._host = host
        self._machine = machine  # whose handlers the messages that change the state go to
        self._fd: int | None = None
        self._handlers: dict[object, tuple[Gst.MessageType, MessageCallback]] = {}

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Read the bus on `loop`."""
        fd = self._host.bus.get_pollfd().fd
        loop.add_reader(fd, self.drain)
        self._fd = fd

    def release(self) -> int | None:
        """Forget the fd the loop reads, and return it: a loop still running stops reading it
        with `detach`."""
        fd, self._fd = self._fd, None
        return fd

    @staticmethod
    def detach(loop: asyncio.AbstractEventLoop, fd: int | None) -> None:
        """Stop reading `fd`, the one `release` returned, on `loop`, which still runs."""
        if fd is not None:
            loop.remove_reader(fd)

    def add_handler(
        self, message_type: Gst.MessageType, callback: MessageCallback
    ) -> Callable[[], None]:
        token = object()
        self._handlers[token] = (message_type, callback)

        def remove() -> None:
            self._handlers.pop(token, None)

        return remove

    # -- bus message handling -- #

    def drain(self) -> None:
        """The loop's reader callback: handle every message on the bus."""
        while (message := self._host.bus.pop()) is not None:
            self._handle(message)

    def _handle(self, message: Gst.Message) -> None:
        kind = message.type
        if kind == Gst.MessageType.ERROR:
            self._machine.on_error(message)
        elif kind == Gst.MessageType.EOS:
            self._machine.on_eos()
        elif kind == Gst.MessageType.STATE_CHANGED:
            self._machine.on_state_changed(message)
        elif kind == Gst.MessageType.WARNING:
            self._on_warning(message)
        elif kind == Gst.MessageType.CLOCK_LOST:
            self._machine.on_clock_lost()
        elif kind == Gst.MessageType.LATENCY:
            self._host.pipeline.recalculate_latency()
        elif kind == Gst.MessageType.BUFFERING:
            self._machine.on_buffering(message)
        elif kind == Gst.MessageType.QOS:
            logger.debug("QoS from %s", message.src.get_name())
        self._host._publish(events.Message(message))
        for message_type, callback in tuple(self._handlers.values()):
            if kind & message_type:
                try:
                    callback(message)
                except Exception:
                    logger.exception("Message handler %r failed", callback)

    def _on_warning(self, message: Gst.Message) -> None:
        gerror, debug = message.parse_warning()
        source = message.src.get_name() if message.src else "unknown"
        logger.warning("Warning from element %s: %s (%s)", source, gerror.message, debug)
        self._host._publish(events.Warning(source, gerror.message, debug))
