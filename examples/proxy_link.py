"""Two players in one asyncio loop, linked by proxysink and proxysrc on one clock and one base
time, and what the start order and the base time do.

python3 examples/proxy_link.py
"""

# --8<-- [start:link]
import asyncio
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player

HEAD_START = 1.5  # seconds the producer plays before the consumer starts, in `first_frame`
PRODUCER = "videotestsrc is-live=true ! video/x-raw,framerate=10/1 ! proxysink name=out"
CONSUMER = "proxysrc name=in ! queue ! fakesink name=sink sync=true"


def link(producer: Player, consumer: Player, *, share_base_time: bool = True) -> None:
    """Tell proxysrc which proxysink it takes from, and put both pipelines on one timeline."""
    psrc = consumer.pipeline.get_by_name("in")
    psrc.set_property("proxysink", producer.pipeline.get_by_name("out"))
    clock = Gst.SystemClock.obtain()
    base_time = clock.get_time()
    for player in (producer, consumer):
        player.pipeline.use_clock(clock)
        if share_base_time:
            # NONE: do not reset the base time when the pipeline goes to PLAYING
            player.pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
            player.pipeline.set_base_time(base_time)


# --8<-- [end:link]


# --8<-- [start:order]
async def start_order() -> bool:
    """Is the consumer's play() still waiting before the producer plays?"""
    producer = Player.from_description(PRODUCER)
    consumer = Player.from_description(CONSUMER)
    link(producer, consumer)
    async with producer, consumer:
        consumer_playing = asyncio.create_task(consumer.play())
        await asyncio.wait([consumer_playing], timeout=1)
        waiting = not consumer_playing.done()
        await producer.play()
        await consumer_playing
    return waiting


# --8<-- [end:order]


# --8<-- [start:eos]
async def lossless() -> bool:
    """Did the consumer render all 50 buffers of the producer, and get its EOS?"""
    producer = Player.from_description(
        "videotestsrc num-buffers=50 ! video/x-raw,framerate=50/1 ! proxysink name=out"
    )
    consumer = Player.from_description(CONSUMER)
    link(producer, consumer)
    sink = consumer.pipeline.get_by_name("sink")
    async with producer, consumer:
        # proxysink drops what it pushes before proxysrc is active: activate it first (the
        # pipeline returns ASYNC at once, as it cannot preroll before the producer plays)
        consumer.pipeline.set_state(Gst.State.PAUSED)
        await asyncio.gather(producer.play(), consumer.play())
        await asyncio.wait_for(consumer.play_until_eos(), 10)
        return bool(sink.get_property("stats").get_value("rendered") == 50)


# --8<-- [end:eos]


# --8<-- [start:lag]
async def first_frame(*, share_base_time: bool) -> float:
    """Seconds from the consumer's play() until its sink has rendered a frame, when the
    producer has been playing for HEAD_START seconds before it."""
    producer = Player.from_description(PRODUCER)
    consumer = Player.from_description(CONSUMER)
    link(producer, consumer, share_base_time=share_base_time)
    sink = consumer.pipeline.get_by_name("sink")
    async with producer, consumer:
        await producer.play()
        await asyncio.sleep(HEAD_START)
        started = time.monotonic()
        await consumer.play()
        while sink.get_property("stats").get_value("rendered") == 0:
            await asyncio.sleep(0.05)
        return time.monotonic() - started


# --8<-- [end:lag]


# --8<-- [start:main]
async def main() -> None:
    print(f"the consumer waits for the producer to play: {await start_order()}")
    # > the consumer waits for the producer to play: True
    print(f"all 50 buffers and the EOS arrive: {await lossless()}")
    # > all 50 buffers and the EOS arrive: True
    shared = await first_frame(share_base_time=True)
    print(f"shared base time: first frame before the producer's age: {shared < HEAD_START}")
    # > shared base time: first frame before the producer's age: True
    own = await first_frame(share_base_time=False)
    print(f"own base time: first frame after the producer's age: {own >= HEAD_START}")
    # > own base time: first frame after the producer's age: True


if __name__ == "__main__":
    asyncio.run(main())
# --8<-- [end:main]
