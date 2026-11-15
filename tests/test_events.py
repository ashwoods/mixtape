# type: ignore
import asyncio
import dataclasses

import pytest

from mixtape.events import Broadcast, Eos, Event, Position, PropertyChanged


@pytest.mark.asyncio
async def test_every_subscriber_gets_every_event():
    broadcast = Broadcast()
    first, second = broadcast.subscribe(), broadcast.subscribe()
    broadcast.publish(Eos())
    broadcast.close()
    assert [e async for e in first] == [Eos()]
    assert [e async for e in second] == [Eos()]


@pytest.mark.asyncio
async def test_only_position_streams_receive_positions():
    broadcast = Broadcast()
    plain, positions = broadcast.subscribe(), broadcast.subscribe(positions=True)
    assert broadcast.wants_positions
    broadcast.publish(Position(1.0, None))
    broadcast.publish(Eos())
    broadcast.close()
    assert [e async for e in plain] == [Eos()]
    assert [e async for e in positions] == [Position(1.0, None), Eos()]
    assert not broadcast.wants_positions


@pytest.mark.asyncio
async def test_a_closed_stream_ends_and_stops_receiving():
    broadcast = Broadcast()
    stream = broadcast.subscribe()
    stream.close()
    stream.close()
    broadcast.publish(Eos())
    assert [e async for e in stream] == []
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), 5)


def test_events_are_frozen():
    assert issubclass(Eos, Event)
    # a field: on Python 3.12, setting a name that is no field of a slotted frozen dataclass
    # raises TypeError instead
    with pytest.raises(dataclasses.FrozenInstanceError):
        PropertyChanged("src.pattern", "src", "pattern", "snow").value = "smpte"
