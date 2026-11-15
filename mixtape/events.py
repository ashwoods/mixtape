"""Events a `Player` publishes, and the stream subscribers read them from."""

import asyncio
from dataclasses import dataclass
from typing import Self

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .exceptions import PlayerPipelineError


@dataclass(frozen=True, slots=True)
class Event:
    """Base class of every event published by a `Player`."""


@dataclass(frozen=True, slots=True)
class StateChanged(Event):
    """The pipeline itself changed state."""

    old: Gst.State
    new: Gst.State
    pending: Gst.State


@dataclass(frozen=True, slots=True)
class Eos(Event):
    """End of stream."""


@dataclass(frozen=True, slots=True)
class Error(Event):
    """A pipeline error. The player has already set the pipeline to NULL."""

    error: PlayerPipelineError


@dataclass(frozen=True, slots=True)
class Warning(Event):
    """A pipeline warning."""

    source: str
    message: str
    debug: str | None


@dataclass(frozen=True, slots=True)
class Message(Event):
    """Any bus message, published after the specific event (if any) for it."""

    message: Gst.Message


class EventStream:
    """Async iterator over the events published after it was created.

    It ends when the player is torn down or `close()` is called, after
    the events already queued have been read.
    """

    def __init__(self, streams: set["EventStream"]) -> None:
        self._streams = streams
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        streams.add(self)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)  # any other reader of this stream ends too
            raise StopAsyncIteration
        return event

    def put(self, event: Event) -> None:
        self._queue.put_nowait(event)

    def close(self) -> None:
        """Stop receiving events; the iteration ends once the queued ones are read."""
        if self in self._streams:
            self._streams.remove(self)
            self._queue.put_nowait(None)


class Broadcast:
    """Fan out events to any number of `EventStream` subscribers."""

    def __init__(self) -> None:
        self._streams: set[EventStream] = set()

    def subscribe(self) -> EventStream:
        return EventStream(self._streams)

    def publish(self, event: Event) -> None:
        for stream in tuple(self._streams):
            stream.put(event)

    def close(self) -> None:
        """End every subscriber's iteration."""
        for stream in tuple(self._streams):
            stream.close()
